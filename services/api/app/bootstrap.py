import sys, hashlib, shutil
from pathlib import Path
from sqlalchemy import select
from .config import DATABASE_URL, ROOT, PRIVATE

def main():
    if '--seed' not in sys.argv:
        import psycopg
        url=DATABASE_URL.replace('postgresql+psycopg://','postgresql://')
        from urllib.parse import urlsplit, urlunsplit
        parts=urlsplit(url); dbname=parts.path.lstrip('/')
        admin=urlunsplit(parts._replace(path='/postgres'))
        with psycopg.connect(admin, autocommit=True) as conn:
            if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(dbname,)).fetchone():
                from psycopg import sql
                conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(dbname)))
        print('Project database ready.')
        return
    from .db import Session, Account, Setting, Batch, Job, Quote
    from datetime import datetime
    with Session.begin() as s:
        if not s.get(Setting,'ledger_version'): s.add(Setting(key='ledger_version',value={'n':0}))
        if not s.get(Setting,'auth'): s.add(Setting(key='auth',value={}))
        if not s.get(Account,'okx'):
            s.add_all([Account(id='okx',name='OKX · 交易账户',platform='OKX',currency='USDT'),
              Account(id='moomoo',name='moomoo · 美股',platform='moomoo',currency='USD'),
              Account(id='alipay',name='支付宝 · 基金',platform='Alipay',currency='CNY')])
    print('Empty platform sources prepared; import your own statements in the app.')

if __name__=='__main__': main()
