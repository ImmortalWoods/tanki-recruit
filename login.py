#!/usr/bin/env python3
"""Ручной вход в Lesta с отдельным постоянным профилем Chromium."""
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

PAGE_URL = 'https://lesta.ru/clans/wot/recruit_members/'
DATA_DIR = Path(os.environ.get('TANKI_RECRUIT_DATA', Path.home() / '.local/share/tanki-recruit'))
PROFILES_DIR = DATA_DIR / 'profiles'


def main():
    if len(sys.argv) == 1:
        accounts = sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir()) if PROFILES_DIR.exists() else []
        print('Аккаунты: ' + (', '.join(accounts) if accounts else 'пока нет'))
        print('Вход: python login.py <имя>')
        return 0
    if len(sys.argv) != 2 or not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', sys.argv[1]) or sys.argv[1] in ('.', '..'):
        print('Укажите имя аккаунта: до 32 латинских букв, цифр, точек, дефисов или подчёркиваний.', file=sys.stderr)
        return 2
    from playwright.sync_api import sync_playwright

    PROFILES_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = PROFILES_DIR / sys.argv[1]
    profile.mkdir(mode=0o700, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile), headless=False, viewport={'width': 1400, 'height': 900}, locale='ru-RU')
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(PAGE_URL, wait_until='domcontentloaded', timeout=60000)
            print('Войдите в Lesta в открывшемся окне, затем нажмите Enter здесь.')
            input()
            page.goto(PAGE_URL, wait_until='domcontentloaded', timeout=60000)
            url = urlsplit(page.url)
            if url.hostname != 'lesta.ru' or url.path.rstrip('/') != '/clans/wot/recruit_members':
                print('Не удалось подтвердить вход: страница набора недоступна для этой сессии.', file=sys.stderr)
                return 1
            print('Страница набора открыта; профиль сохранён:', profile)
            return 0
        finally:
            context.close()


if __name__ == '__main__':
    sys.exit(main())
