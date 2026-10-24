"""Rotate only this project's local database role; never print connection secrets."""
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit, urlunsplit, unquote
import psycopg
from psycopg import sql

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'services/api'))
from app.config import DATABASE_URL

def main():
    url=DATABASE_URL.replace('postgresql+psycopg://','postgresql://')
    parts=urlsplit(url)
    if (parts.hostname,parts.port,parts.username,parts.path)!=('127.0.0.1',55432,'portfolio_owner','/portfolio'):
        raise ValueError('Only the project-specific local database may be rotated')
    path=root/'.env';original=path.read_text(encoding='utf-8-sig')
    replacement=secrets.token_hex(32)
    new_url=urlunsplit(parts._replace(netloc=f'portfolio_owner:{replacement}@127.0.0.1:55432'))
    new_config=new_url.replace('postgresql://','postgresql+psycopg://',1)
    changed,count=re.subn(r'^DATABASE_URL=.*$',lambda m:'DATABASE_URL='+new_config,original,flags=re.MULTILINE)
    if count!=1:raise ValueError('Expected exactly one DATABASE_URL entry')
    temporary=root/'.env.rotate.tmp'
    with psycopg.connect(url) as conn:
        try:
            with conn.transaction():
                before_count=conn.execute('SELECT count(*) FROM events').fetchone()[0]
                conn.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier('portfolio_owner'),sql.Literal(replacement)))
                temporary.write_text(changed,encoding='utf-8');os.replace(temporary,path)
            with psycopg.connect(new_url) as verify:
                if verify.execute('SELECT count(*) FROM events').fetchone()[0]!=before_count:raise ValueError('Unexpected ledger count')
        except Exception:
            conn.rollback()
            with conn.transaction():conn.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier('portfolio_owner'),sql.Literal(unquote(parts.password))))
            temporary.write_text(original,encoding='utf-8');os.replace(temporary,path)
            raise
    print('Project database credential rotated and connection verified. No credential printed.')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Rotation not completed:',type(exc).__name__);sys.exit(1)
