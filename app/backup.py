#!/usr/bin/env python3
"""Dated copies of the files that cannot be rebuilt.

The aircraft history database and the arrival ledgers only ever grow; nothing
upstream keeps them, so a lost file is lost for good. Once a day this writes a
copy of each into AVIATION_BACKUP_DIR/<YYYY-MM-DD>/ and removes the oldest
folders beyond AVIATION_BACKUP_KEEP (default 3).

The database is copied with SQLite's own backup call, which gives a consistent
copy while the collector keeps writing. Off unless AVIATION_BACKUP_DIR is set.
"""

import glob
import os
import shutil
import sqlite3
import sys
from datetime import datetime

import settings

settings.apply_timezone()

BACKUP_DIR = settings.get('AVIATION_BACKUP_DIR')
KEEP = settings.get_int('AVIATION_BACKUP_KEEP', 3)


def ledger_files():
    files = [settings.record_file(icao) for icao in settings.AIRPORT_CODES]
    files.append(settings.data_path('flight_patterns.json'))
    files.append(settings.data_path('adsb_coverage.json'))
    return [f for f in files if os.path.isfile(f)]


def backup_database(src, dst):
    source = sqlite3.connect(f'file:{src}?mode=ro', uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def main():
    if not BACKUP_DIR:
        print('no backup folder set (AVIATION_BACKUP_DIR); backups are off')
        return 0
    day = datetime.now(settings.TZ).strftime('%Y-%m-%d')
    folder = os.path.join(BACKUP_DIR, day)
    os.makedirs(folder, exist_ok=True)

    copied = []
    if os.path.isfile(settings.DB_PATH):
        tmp = os.path.join(folder, 'adsb_history.db.tmp')
        backup_database(settings.DB_PATH, tmp)
        os.replace(tmp, os.path.join(folder, 'adsb_history.db'))
        copied.append('adsb_history.db')
    for path in ledger_files():
        shutil.copy2(path, os.path.join(folder, os.path.basename(path)))
        copied.append(os.path.basename(path))

    # Dated folders only; anything else in the backup folder is left alone.
    dated = sorted(d for d in glob.glob(os.path.join(BACKUP_DIR, '????-??-??'))
                   if os.path.isdir(d))
    for old in dated[:-KEEP] if KEEP > 0 else []:
        shutil.rmtree(old)
        print(f'removed old backup {os.path.basename(old)}')
    print(f'backup {day}: {", ".join(copied) or "nothing to copy"} -> {folder}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
