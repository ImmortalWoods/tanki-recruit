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


def emit(*values, **kwargs):
    """Вывести событие и сохранить его в ежедневном журнале по местному времени."""
    now = datetime.now().astimezone()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    message = ' '.join(str(value) for value in values)
    with (LOG_DIR / now.strftime('%d.%m.%Y.log')).open('a', encoding='utf-8') as log:
        log.write(f'{now.isoformat(timespec="seconds")} {message}\n')
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
    parser.add_argument('--limit', type=int, default=10, help='максимум приглашений за запуск (по умолчанию 10)')
    parser.add_argument('--pending', action='store_true', help='показать неоднозначные попытки')
    parser.add_argument('--resolve-pending', type=int, metavar='ID', help='разрешить неоднозначную попытку после проверки на сайте')
    parser.add_argument('--resolution', choices=('sent', 'retry'), help='sent: приглашение найдено; retry: его нет и можно повторить')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit должен быть положительным')
    if (args.resolve_pending is None) != (args.resolution is None):
        parser.error('--resolve-pending и --resolution используются вместе')
    if (args.pending or args.resolve_pending is not None) and args.send:
        parser.error('управление незавершёнными попытками несовместимо с --send')
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = lock_file(DATA_DIR / 'recruit.lock')
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
                    emit(f'Уровень {level}, период {period}: {len(seen)} игроков, запросов {MAX_PAGES}', flush=True)
            if not rows:
                raise RuntimeError('Сайт не вернул ни одного кандидата; проверьте доступ и настройки набора')
            missing = [player_id for player_id, data in rows.items() if 'all' not in data or '28' not in data]
            invalid.update(missing)
            if invalid:
                emit(f'Пропущено игроков с неполной или некорректной статистикой: {len(invalid)}', flush=True)
            if len(invalid) == len(rows):
                raise RuntimeError('Нет кандидатов с полной статистикой за оба периода')
            sent = 0
            matched = 0
            active_skipped = 0
            for player_id, data in rows.items():
                if player_id in invalid:
                    continue
                lifetime, recent = data['all']['statistics'], data['28']['statistics']
                battles, wins, recent_battles = lifetime['btl'], lifetime['wb'], recent['btl']
                if battles >= MIN_BATTLES and wins >= MIN_WIN_PERCENT and recent_battles >= MIN_BATTLES_PER_DAY * PERIOD_DAYS:
                    matched += 1
                    player = data['all']
                    emit(json.dumps({'id': player['id'], 'name': player['account']['name'], 'battles': battles, 'wins_percent': round(wins, 2), 'battles_28_days': recent_battles, 'per_day': round(recent_battles / PERIOD_DAYS, 2)}, ensure_ascii=False), flush=True)
                    if args.send and sent < args.limit:
                        if not page.evaluate("document.cookie.split('; ').some(x => x.startsWith('csrftoken='))"):
                            raise RuntimeError('Не найден CSRF-токен; повторите вход')
                        before = player_invitations(page, player['id'])
                        if 'active' in before.values():
                            active_skipped += 1
                            emit('У игрока уже есть активное приглашение:', player['account']['name'], player['id'], flush=True)
                            continue
                        emit('Попытка отправки:', player['account']['name'], player['id'], 'предыдущие статусы:', ', '.join(sorted(set(before.values()))) or 'нет', flush=True)
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
                        emit('Приглашение подтверждено:', player['account']['name'], player['id'], flush=True)
                        sent += 1
                        time.sleep(2)
            emit(f'Итог: игроков {len(rows)}, подошло {matched}, отправлено {sent}, активных приглашений пропущено {active_skipped}, без полной статистики {len(invalid)}', flush=True)
        finally:
            browser.close()

if __name__ == '__main__':
    try:
        emit('Запуск:', ' '.join(sys.argv[1:]) or 'поиск без отправки', flush=True)
        main()
        emit('Завершено успешно', flush=True)
    except Exception as exc:
        emit('Ошибка:', str(exc), file=sys.stderr, flush=True)
        raise
