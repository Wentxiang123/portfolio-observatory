"""Back up local data, initialize SQLite identity, and verify financial rows are unchanged."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote
from upgrade_v03 import financial_fingerprint

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'services/api'))
from app.config import DATABASE_URL, PRIVATE
from app.auth_migration import migrate
from app import auth_store

def main():
    before=financial_fingerprint()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder=PRIVATE/'backups'/('before-v04-'+stamp)
    folder.mkdir(parents=True,exist_ok=False)
    parts=urlsplit(DATABASE_URL.replace('postgresql+psycopg://','postgresql://'))
    pg_dump=Path(os.environ.get('PG_BIN',r'C:\Program Files\PostgreSQL\17\bin'))/'pg_dump.exe'
    env={**os.environ,'PGPASSWORD':unquote(parts.password or '')}
    result=subprocess.run([str(pg_dump),'-h',parts.hostname,'-p',str(parts.port or 5432),'-U',unquote(parts.username),'-d',parts.path.lstrip('/'),'-Fc','-f',str(folder/'portfolio.dump')],env=env,capture_output=True)
    if result.returncode or not (folder/'portfolio.dump').stat().st_size:
        raise RuntimeError('Financial backup failed; authentication migration not started')
    dbpath=auth_store.AUTH_DIR/'auth.sqlite3'
    if dbpath.exists():
        with sqlite3.connect(dbpath) as source,sqlite3.connect(folder/'auth.sqlite3') as dest:source.backup(dest)
        keypath=auth_store.AUTH_DIR/'masterkey.dpapi'
        if keypath.exists():shutil.copy2(keypath,folder/keypath.name)
    migrate()
    after=financial_fingerprint()
    if before!=after:raise RuntimeError('Financial fingerprint differs; preserve backup and investigate')
    (folder/'verification.json').write_text(json.dumps({'before':before,'after':after,'auth_migration':'SQLite v1'},indent=2),encoding='utf-8')
    print('v0.4 identity initialized; financial rows unchanged:',after['events'])
    print('Backup folder:',folder)
    print('Use the original password in the account upgrade screen; no real TOTP has been enrolled.')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Upgrade failed:',type(exc).__name__,'— connection credentials are not logged.');raise SystemExit(1)
