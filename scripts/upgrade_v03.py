"""Back up PostgreSQL before migration, then assert financial records are unchanged."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'services/api'))
from app.config import DATABASE_URL, PRIVATE
from app.db import engine
from sqlalchemy import text

def financial_fingerprint():
    with engine.connect() as c:
        rows=c.execute(text('SELECT id,account_id,batch_id,source_id,occurred_at,kind,symbol,currency,quantity,price,fee,fee_currency,active,evidence FROM events ORDER BY id')).all()
        value=[list(r) for r in rows]
        digest=hashlib.sha256(json.dumps(value,default=str,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        return {'events':len(rows),'sha256':digest}

def main():
    before=financial_fingerprint()
    parts=urlsplit(DATABASE_URL.replace('postgresql+psycopg://','postgresql://'))
    folder=PRIVATE/'backups';folder.mkdir(exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target=folder/('before-v03-'+stamp+'.dump')
    pg_dump=Path(os.environ.get('PG_BIN',r'C:\Program Files\PostgreSQL\17\bin'))/'pg_dump.exe'
    env={**os.environ,'PGPASSWORD':unquote(parts.password or '')}
    result=subprocess.run([str(pg_dump),'-h',parts.hostname,'-p',str(parts.port or 5432),'-U',unquote(parts.username),'-d',parts.path.lstrip('/'),'-Fc','-f',str(target)],env=env,capture_output=True)
    if result.returncode or not target.exists() or target.stat().st_size==0:
        raise RuntimeError('Backup failed; migration was not started')
    env.pop('PGPASSWORD',None);env['PYTHONPATH']=str(root/'services/api')
    subprocess.run([sys.executable,'-m','alembic','-c','migrations/alembic.ini','upgrade','head'],cwd=root,env=env,check=True)
    after=financial_fingerprint()
    if before!=after:raise RuntimeError('Financial record verification failed; keep backup and investigate')
    (folder/('verified-v03-'+stamp+'.json')).write_text(json.dumps({'backup':target.name,'before':before,'after':after},indent=2),encoding='utf-8')
    print('Migration complete. Financial rows unchanged:',after['events'])
    print('Backup:',target)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Upgrade failed:',type(exc).__name__,'— no connection credentials are logged.');sys.exit(1)
