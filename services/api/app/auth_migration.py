"""One-time, non-destructive import of v0.3 owner claims; old HTTP sessions are not migrated."""
import json
from sqlalchemy import select
from .db import Session, User, Setting
from . import auth_store as store

def migrate():
    store.initialize()
    with store.connection() as c:
        if store.meta(c,'v03_imported')=='yes':return
        records=[]
        with Session() as s:
            for user in s.scalars(select(User)):
                records.append((user.id,user.username.strip().casefold(),{'hash':user.password_hash,'salt':user.salt,'n':32768,'p':3}))
            old=s.get(Setting,'auth')
            if old and old.value.get('hash') and not any(r[0]=='legacy' for r in records):
                v=old.value
                records.append(('legacy','owner',{'hash':v['hash'],'salt':v['salt'],'n':v.get('n',16384),'p':v.get('p',1)}))
        for owner,name,value in records:
            c.execute('INSERT INTO legacy_accounts(owner_id,username,credential) VALUES (?,?,?)',
                      (owner,name,store.encrypt(json.dumps(value))))
        c.execute("INSERT INTO metadata VALUES ('v03_imported','yes')")
        store.audit(c,None,'v03_owner_claims_imported')

if __name__=='__main__':
    try:migrate();print('Local SQLite authentication initialized. Existing portfolios preserved; no sessions or TOTP enrolled.')
    except Exception as exc:
        print('Authentication initialization failed:',type(exc).__name__);raise SystemExit(1)
