#!/usr/bin/env python3
"""Поиск кандидатов и, по запросу, отправка приглашений."""
import json
import argparse
import time
import sqlite3
import fcntl
from login import DATA_DIR
from login import PAGE_URL, PROFILES_DIR
from playwright.sync_api import sync_playwright

API = '/clans/wot/recruitstation/api/recommended_accounts/'

# Критерии отбора: первые два — за всё время, активность — за 28 дней.
MIN_BATTLES = 5000
MIN_WIN_PERCENT = 53
MIN_BATTLES_PER_DAY = 5
PERIOD_DAYS = 28
PAGE_SIZE = 100
MAX_PAGES = 100
EMPTY_PAGES_TO_FINISH = 3

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send', action='store_true', help='отправить приглашения найденным игрокам')
    parser.add_argument('--limit', type=int, default=10, help='максимум приглашений за запуск (по умолчанию 10)')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit должен быть положительным')
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(DATA_DIR / 'recruit.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Другой запуск уже работает')
    db = sqlite3.connect(DATA_DIR / 'invitations.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS invitations (id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    db.commit()
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(str(PROFILES_DIR / 'main'), headless=True, locale='ru-RU')
        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(PAGE_URL, wait_until='domcontentloaded', timeout=60000)
            if page.url.rstrip('/') != PAGE_URL.rstrip('/'):
                raise RuntimeError('Нужен повторный вход: python login.py main')
            rows = {}
            for level in range(3):
                for period in ('all', '28'):
                    empty_pages = 0
                    seen = set()
                    for page_number in range(MAX_PAGES):
                        offset = page_number * PAGE_SIZE
                        result = page.evaluate('''async ({url, level, period, offset, limit}) => {
                          let p = new URLSearchParams({offset, limit, period, battle_type: 'default', compliance_level: level});
                          let r = await fetch(url + '?' + p, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
                          if (!r.ok) throw Error('HTTP ' + r.status);
                          return await r.json();
                        }''', {'url': API, 'level': str(level), 'period': period, 'offset': offset, 'limit': PAGE_SIZE})
                        if not isinstance(result.get('accounts'), list) or not isinstance(result.get('_meta_'), dict):
                            raise RuntimeError('Неожиданный ответ сайта')
                        accounts = result['accounts']
                        ids = {item['id'] for item in accounts}
                        if accounts and not ids.difference(seen):
                            raise RuntimeError(f'Сайт повторил страницу: уровень {level}, период {period}, offset {offset}')
                        seen.update(ids)
                        for item in accounts:
                            rows.setdefault(item['id'], {})[period] = item
                        empty_pages = empty_pages + 1 if not accounts else 0
                        if empty_pages >= EMPTY_PAGES_TO_FINISH:
                            break
                    else:
                        raise RuntimeError(f'Достигнут предел страниц: уровень {level}, период {period}')
                    print(f'Уровень {level}, период {period}: {len(seen)} игроков, запросов {page_number + 1}', flush=True)
            sent = 0
            for data in rows.values():
                if 'all' not in data or '28' not in data:
                    continue
                lifetime = data['all']['statistics']
                recent = data['28']['statistics']
                battles, wins, recent_battles = lifetime.get('btl'), lifetime.get('wb'), recent.get('btl')
                if all(isinstance(x, (int, float)) for x in (battles, wins, recent_battles)) and battles >= MIN_BATTLES and wins >= MIN_WIN_PERCENT and recent_battles >= MIN_BATTLES_PER_DAY * PERIOD_DAYS:
                    player = data['all']
                    print(json.dumps({'name': player['account']['name'], 'battles': battles, 'wins_percent': round(wins, 2), 'battles_28_days': recent_battles, 'per_day': round(recent_battles / PERIOD_DAYS, 2)}, ensure_ascii=False), flush=True)
                    if args.send and sent < args.limit:
                        if db.execute('SELECT 1 FROM invitations WHERE id = ?', (player['id'],)).fetchone():
                            print('Уже обработан ранее:', player['account']['name'], flush=True)
                            continue
                        db.execute('INSERT INTO invitations (id, name, status) VALUES (?, ?, ?)', (player['id'], player['account']['name'], 'pending'))
                        db.commit()
                        outcome = page.evaluate('''async (id) => {
                          const token = document.cookie.split('; ').find(x => x.startsWith('csrftoken='))?.split('=').slice(1).join('=');
                          if (!token) throw Error('Не найден CSRF-токен');
                          const r = await fetch('/clans/wot/recruitstation/api/invites/', {
                            method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': decodeURIComponent(token)},
                            body: JSON.stringify({comment: '', recipient_id: id, source: 'clans_portal.recruitstation'})
                          });
                          return {status: r.status, body: (await r.text()).slice(0, 1000)};
                        }''', player['id'])
                        if outcome['status'] not in (200, 201):
                            raise RuntimeError(f"Отправка остановлена: HTTP {outcome['status']}: {outcome['body']}")
                        db.execute('UPDATE invitations SET status = ? WHERE id = ?', ('sent', player['id']))
                        db.commit()
                        print('Приглашение отправлено:', player['account']['name'], flush=True)
                        sent += 1
                        time.sleep(2)
        finally:
            browser.close()

if __name__ == '__main__':
    main()
