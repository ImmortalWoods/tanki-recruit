"""Пути данных и блокировка процесса на Linux и Windows."""
import os
from pathlib import Path


def data_dir():
    if os.environ.get('TANKI_RECRUIT_DATA'):
        return Path(os.environ['TANKI_RECRUIT_DATA'])
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'tanki-recruit'
    return Path.home() / '.local' / 'share' / 'tanki-recruit'


def lock_file(path):
    """Вернуть открытый файл блокировки, который нужно держать до конца запуска."""
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise SystemExit('Другой запуск уже работает или файл блокировки недоступен')
    return handle
