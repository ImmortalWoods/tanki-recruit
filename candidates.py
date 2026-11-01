#!/usr/bin/env python3
"""Поиск кандидатов и, по запросу, отправка приглашений."""
import argparse
from datetime import datetime
from pathlib import Path
import sys
import json
import math
import sqlite3
import time

from platform_bits import lock_file
from login import DATA_DIR, PAGE_URL, PROFILES_DIR
from playwright.sync_api import sync_playwright

API = '/clans/wot/recruitstation/api/recommended_accounts/'
INVITES_API = '/clans/wot/recruitstation/api/clan_invites/'

# Критерии отбора: первые два — за всё время, активность — за 28 дней.
MIN_BATTLES = 5000
MIN_WIN_PERCENT = 53
MIN_BATTLES_PER_DAY = 5
PERIOD_DAYS = 28
PAGE_SIZE = 100
# Сайт не возвращает общее число рекомендаций. Проверяется этот диапазон страниц;
# даже после пустых страниц за его пределами могут оставаться игроки.
MAX_PAGES = 100
LOG_DIR = Path(__file__).resolve().parent / 'logs'
RUN_MARKER = DATA_DIR / 'recruit-run.txt'
RUN_LOCK = None
RUN_STARTED = False


def emit(*values, **kwargs):
    """Вывести событие и сохранить его в ежедневном журнале по местному времени."""
    now = datetime.now().astimezone()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    message = ' '.join(str(value) for value in values)
    with (LOG_DIR / now.strftime('%d.%m.%Y.log')).open('a', encoding='utf-8') as log:
        log.write(f'{now:%d.%m.%Y %H:%M:%S}  {message}\n')
    print(*values, **kwargs)


def player_invitations(page, player_id):
    """Получить ID и статусы приглашений игрока; ошибка означает неопределённость."""
    invitations = {}
    offset = 0
    total = None
    while total is None or offset < total:
        result = page.evaluate('''async ({url, offset, limit}) => {
          const p = new URLSearchParams({offset, limit});
          const r = await fetch(url + '?' + p, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
          if (!r.ok) throw Error('HTTP ' + r.status);
          return await r.json();
        }''', {'url': INVITES_API, 'offset': offset, 'limit': PAGE_SIZE})
        if not isinstance(result, dict) or not isinstance(result.get('_meta_'), dict) or not isinstance(result.get('invites'), list):
            raise RuntimeError('Неожиданный ответ списка приглашений')
        meta, invites = result['_meta_'], result['invites']
        current_total = meta.get('total')
        if type(current_total) is not int or current_total < 0 or meta.get('offset') != offset or (total is not None and current_total != total):
            raise RuntimeError('Список приглашений изменился или имеет неверную пагинацию; повторите проверку')
        total = current_total
        if (not invites and offset < total) or len(invites) > PAGE_SIZE or offset + len(invites) > total:
            raise RuntimeError('Неполный список приглашений')
        for invite in invites:
            if not isinstance(invite, dict) or type(invite.get('id')) is not int or not isinstance(invite.get('account'), dict) or type(invite['account'].get('id')) is not int:
                raise RuntimeError('Неполные данные списка приглашений')
            if invite['account']['id'] == player_id:
                status = invite.get('status')
                if status not in ('active', 'expired', 'declined', 'accepted'):
                    raise RuntimeError(f'Неизвестный статус приглашения игрока {player_id}: {status!r}')
                invitations[invite['id']] = status
        offset += len(invites)
    return invitations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send', action='store_true', help='отправить приглашения найденным игрокам')
    parser.add_argument('--pending', action='store_true', help='показать неоднозначные попытки')
    parser.add_argument('--resolve-pending', type=int, metavar='ID', help='разрешить неоднозначную попытку после проверки на сайте')
    parser.add_argument('--resolution', choices=('sent', 'retry'), help='sent: приглашение найдено; retry: его нет и можно повторить')
    args = parser.parse_args()
    if (args.resolve_pending is None) != (args.resolution is None):
        parser.error('--resolve-pending и --resolution используются вместе')
    if (args.pending or args.resolve_pending is not None) and args.send:
        parser.error('управление незавершёнными попытками несовместимо с --send')
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    global RUN_LOCK, RUN_STARTED
    RUN_LOCK = lock_file(DATA_DIR / 'recruit.lock')
    previous = RUN_MARKER.read_text(encoding='utf-8').strip() if RUN_MARKER.exists() else None
    RUN_MARKER.write_text(datetime.now().astimezone().isoformat(timespec='seconds'), encoding='utf-8')
    RUN_STARTED = True
    emit('=' * 48)
    if previous:
        emit(f'Внимание: предыдущий запуск ({previous}) не записал завершение; причина неизвестна.')
    if args.pending:
        mode = 'проверка незавершённых попыток'
    elif args.resolve_pending is not None:
        mode = 'разрешение незавершённой попытки'
    elif args.send:
        mode = 'поиск и отправка приглашений'
    else:
        mode = 'поиск без отправки'
    emit('Запуск:', mode)
    if not args.pending and args.resolve_pending is None:
        emit(f'Критерии: от {MIN_BATTLES} боёв, победы от {MIN_WIN_PERCENT}%, от {MIN_BATTLES_PER_DAY} боёв/день за {PERIOD_DAYS} дней')
    db = sqlite3.connect(DATA_DIR / 'invitations.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS invitations (id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    db.commit()
    if args.resolve_pending is not None:
        if args.resolution == 'sent':
            cursor = db.execute("UPDATE invitations SET status = 'sent' WHERE id = ? AND status = 'pending'", (args.resolve_pending,))
        else:
            cursor = db.execute("DELETE FROM invitations WHERE id = ? AND status = 'pending'", (args.resolve_pending,))
        if cursor.rowcount != 1:
            raise RuntimeError('Незавершённая попытка с таким ID не найдена')
        db.commit()
        emit('Решение сохранено:', args.resolve_pending, args.resolution)
        return
    pending = db.execute("SELECT id, name, created_at FROM invitations WHERE status = 'pending' ORDER BY created_at").fetchall()
    if args.pending:
        for player_id, name, created in pending:
            emit(f'{player_id}\t{name}\t{created} UTC')
        if not pending:
            emit('Незавершённых попыток нет')
        return
    if pending:
        raise RuntimeError('Есть незавершённые попытки. Выполните --pending и проверьте приглашения на сайте; инструкции в README.')
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(str(PROFILES_DIR / 'main'), headless=True, locale='ru-RU')
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(PAGE_URL, wait_until='domcontentloaded', timeout=60000)
            if page.url.rstrip('/') != PAGE_URL.rstrip('/'):
                raise RuntimeError('Нужен повторный вход: python login.py main')
            rows = {}
            invalid = set()
            emit(f'Проверяется до {MAX_PAGES} страниц на каждый уровень и период; полный охват сайт не гарантирует.', flush=True)
            for level in range(3):
                for period in ('all', '28'):
                    seen = set()
                    for page_number in range(MAX_PAGES):
                        offset = page_number * PAGE_SIZE
                        result = page.evaluate('''async ({url, level, period, offset, limit}) => {
                          let p = new URLSearchParams({offset, limit, period, battle_type: 'default', compliance_level: level});
                          let r = await fetch(url + '?' + p, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
                          if (!r.ok) throw Error('HTTP ' + r.status);
                          return await r.json();
                        }''', {'url': API, 'level': str(level), 'period': period, 'offset': offset, 'limit': PAGE_SIZE})
                        if not isinstance(result, dict) or not isinstance(result.get('accounts'), list) or not isinstance(result.get('_meta_'), dict):
                            raise RuntimeError('Неожиданный ответ сайта')
                        accounts = result['accounts']
                        ids = set()
                        for item in accounts:
                            if not isinstance(item, dict) or type(item.get('id')) is not int or not isinstance(item.get('account'), dict) or not isinstance(item['account'].get('name'), str) or not item['account']['name'] or not isinstance(item.get('statistics'), dict):
                                raise RuntimeError(f'Неполные данные кандидата: уровень {level}, период {period}, offset {offset}')
                            stats = item['statistics']
                            for key in (('btl', 'wb') if period == 'all' else ('btl',)):
                                value = stats.get(key)
                                if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (key == 'wb' and value > 100):
                                    invalid.add(item['id'])
                            ids.add(item['id'])
                            rows.setdefault(item['id'], {})[period] = item
                        if accounts and not ids.difference(seen):
                            raise RuntimeError(f'Сайт повторил страницу: уровень {level}, период {period}, offset {offset}')
                        seen.update(ids)
                        if (page_number + 1) % 10 == 0 and page_number + 1 < MAX_PAGES:
                            emit(f'Загрузка: уровень {level}, {"всё время" if period == "all" else "28 дней"}: страница {page_number + 1}/{MAX_PAGES}', flush=True)
                    emit(f'Загружено: уровень {level}, {"всё время" if period == "all" else "28 дней"} — {len(seen)} игроков, страниц {MAX_PAGES}', flush=True)
            if not rows:
                raise RuntimeError('Сайт не вернул ни одного кандидата; проверьте доступ и настройки набора')
            missing = [player_id for player_id, data in rows.items() if 'all' not in data or '28' not in data]
            invalid.update(missing)
            if invalid:
                emit(f'Пропущено игроков с неполной или некорректной статистикой: {len(invalid)}', flush=True)
            if len(invalid) == len(rows):
                raise RuntimeError('Нет кандидатов с полной статистикой за оба периода')
            sent = 0
            active_skipped = 0
            matches = []
            for player_id, data in rows.items():
                if player_id in invalid:
                    continue
                lifetime, recent = data['all']['statistics'], data['28']['statistics']
                battles, wins, recent_battles = lifetime['btl'], lifetime['wb'], recent['btl']
                if battles >= MIN_BATTLES and wins >= MIN_WIN_PERCENT and recent_battles >= MIN_BATTLES_PER_DAY * PERIOD_DAYS:
                    matches.append((data['all'], battles, wins, recent_battles))
            emit(f'Загрузка завершена: игроков {len(rows)}, подошло {len(matches)}, без полной статистики {len(invalid)}', flush=True)
            for player, battles, wins, recent_battles in matches:
                details = (f"{player['account']['name']} (ID {player['id']}) — боёв: {battles}, "
                           f"побед: {wins:.2f}%, за {PERIOD_DAYS} дней: {recent_battles} "
                           f"({recent_battles / PERIOD_DAYS:.2f} в день)")
                if not args.send:
                    emit('Подходит:', details, flush=True)
                if args.send:
                    if not page.evaluate("document.cookie.split('; ').some(x => x.startsWith('csrftoken='))"):
                        raise RuntimeError('Не найден CSRF-токен; повторите вход')
                    before = player_invitations(page, player['id'])
                    if 'active' in before.values():
                        active_skipped += 1
                        emit('Пропущен — приглашение активно:', details, flush=True)
                        continue
                    status_names = {'expired': 'истекло', 'declined': 'отклонено', 'accepted': 'принято'}
                    history = ', '.join(status_names[status] for status in sorted(set(before.values()))) or 'нет'
                    emit(f'Отправка начата: {details}; предыдущие приглашения: {history}', flush=True)
                    db.execute("INSERT INTO invitations (id, name, status) VALUES (?, ?, 'pending') ON CONFLICT(id) DO UPDATE SET name = excluded.name, status = 'pending', created_at = CURRENT_TIMESTAMP", (player['id'], player['account']['name']))
                    db.commit()
                    outcome = page.evaluate('''async (id) => {
                      const token = document.cookie.split('; ').find(x => x.startsWith('csrftoken='))?.split('=').slice(1).join('=');
                      if (!token) throw Error('Не найден CSRF-токен');
                      const r = await fetch('/clans/wot/recruitstation/api/invites/', {
                        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': decodeURIComponent(token)},
                        body: JSON.stringify({comment: '', recipient_id: id, source: 'clans_portal.recruitstation'})
                      });
                      return {status: r.status, body: await r.text()};
                    }''', player['id'])
                    status = outcome['status']
                    if 400 <= status < 500 and status not in (408, 409, 425):
                        db.execute("DELETE FROM invitations WHERE id = ? AND status = 'pending'", (player['id'],))
                        db.commit()
                        raise RuntimeError(f'Сайт отклонил отправку: HTTP {status}')
                    if status not in (200, 201):
                        raise RuntimeError(f'Неоднозначный результат отправки: HTTP {status}; проверьте --pending')
                    try:
                        body = json.loads(outcome['body'])
                    except (ValueError, TypeError):
                        raise RuntimeError('Ответ отправки не является JSON; проверьте --pending')
                    if not isinstance(body, dict) or body.get('error') or body.get('errors') or body.get('detail'):
                        raise RuntimeError('Ответ отправки не подтверждает успех; проверьте --pending')
                    after = player_invitations(page, player['id'])
                    if not (after.keys() - before.keys()):
                        raise RuntimeError('Новое приглашение не найдено в списке клана; проверьте --pending')
                    db.execute('UPDATE invitations SET status = ? WHERE id = ?', ('sent', player['id']))
                    db.commit()
                    emit(f'Приглашение подтверждено: {player["account"]["name"]} (ID {player["id"]})', flush=True)
                    sent += 1
                    time.sleep(2)
            emit(f'Итог: игроков {len(rows)}, подошло {len(matches)}, отправлено {sent}, активных приглашений пропущено {active_skipped}, без полной статистики {len(invalid)}', flush=True)
        finally:
            browser.close()

if __name__ == '__main__':
    started = time.monotonic()
    try:
        main()
        emit(f'Завершено успешно за {time.monotonic() - started:.0f} сек.', flush=True)
    except KeyboardInterrupt:
        if RUN_STARTED:
            emit('Прервано пользователем. Если отправка была начата, проверьте --pending и список приглашений на сайте.', file=sys.stderr, flush=True)
        raise
    except Exception as exc:
        if RUN_STARTED:
            emit('Ошибка:', str(exc), file=sys.stderr, flush=True)
            emit('Если отправка была начата, проверьте --pending и список приглашений на сайте.', file=sys.stderr, flush=True)
        raise
    finally:
        if RUN_STARTED:
            RUN_MARKER.unlink(missing_ok=True)
