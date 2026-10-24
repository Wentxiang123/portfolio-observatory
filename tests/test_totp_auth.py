"""Security regressions use disposable SQLite + PostgreSQL schemas, never real credentials."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import pyotp
import pytest
from fastapi.testclient import TestClient
from app import auth, auth_store as store
from app.main import app
from app.db import Session, Setting
from test_api import client, login, register, upload
from test_domain import deposit, row

PASSWORD='Local-Test-Only-9384'
NEW_PASSWORD='Replacement-Test-Only-2934'

@pytest.fixture
def tick(monkeypatch):
    stamp=[auth.clock()]
    monkeypatch.setattr(auth,'clock',lambda:stamp[0])
    def advance(seconds=30):
        stamp[0]+=seconds
        return stamp[0]
    return advance

def otp(secret):return pyotp.TOTP(secret).at(auth.clock())
def password_stage(c,password=PASSWORD,name='owner'):
    return c.post('/api/auth/login',json={'username':name,'password':password})
def verify(c,secret):return c.post('/api/auth/login/verify',json={'code':otp(secret)})
def recover(c,codes,**extra):
    return c.post('/api/auth/recover',json={'username':'owner','new_password':NEW_PASSWORD,'recovery_code':codes[0],**extra})

def test_pending_registration_encryption_hashes_and_cookie(client,tick):
    c=client
    r=c.post('/api/auth/enroll/start',json={'username':'owner','password':PASSWORD})
    assert r.status_code==200
    data=r.json();secret=data['manual_key']
    assert data['digits']==6 and data['period']==30 and data['qr_data_url'].startswith('data:image/png;base64,')
    assert c.get('/api/accounts').status_code==401
    assert not c.get('/api/auth').json()['configured']
    with store.connection() as db:
        e=db.execute('SELECT * FROM enrollments').fetchone()
        assert secret not in e['secret'] and store.decrypt(e['secret'])==secret
        assert e['password_hash'].startswith('$argon2id$') and auth.password_ok(e['password_hash'],PASSWORD)
    r=c.post('/api/auth/enroll/confirm',json={'code':otp(secret)})
    assert r.status_code==200
    codes=r.json()['recovery_codes'];assert len(set(codes))==10
    assert 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=strict' in r.headers['set-cookie']
    with store.connection() as db:
        user=db.execute('SELECT * FROM users').fetchone()
        assert store.decrypt(user['totp_secret'])==secret
        assert db.execute('SELECT count(*) FROM enrollments').fetchone()[0]==0
        hashes=[r[0] for r in db.execute('SELECT code_hash FROM recovery_codes')]
        assert all(code not in hashes for code in codes)
        assert db.execute('SELECT token_hash FROM sessions').fetchone()[0]!=c.cookies.get(auth.COOKIE)
    raw=(store.AUTH_DIR/'auth.sqlite3').read_bytes()
    assert secret.encode() not in raw and all(code.encode() not in raw for code in codes)
    assert c.post('/api/auth/enroll/confirm',json={'code':otp(secret)}).status_code==400
    assert 'recovery_codes' not in c.get('/api/auth').json()

def test_two_steps_replay_and_parallel_consumption(client,tick):
    c=client;secret,_=login(c);c.post('/api/logout')
    assert password_stage(c).status_code==200
    assert c.get('/api/accounts').status_code==401
    assert verify(c,secret).status_code==401  # binding already consumed current step
    tick();assert verify(c,secret).status_code==200
    c.post('/api/logout');assert password_stage(c).status_code==200
    assert verify(c,secret).status_code==401
    tick()
    challenge=c.cookies.get(auth.CHALLENGE)
    def attempt():
        with TestClient(app,client=('127.0.0.1',50100)) as other:
            other.cookies.set(auth.CHALLENGE,challenge)
            return verify(other,secret).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:attempt(),range(2)))
    assert sorted(results)==[200,401]

@pytest.mark.parametrize('method',['password','totp','two'])
def test_secure_recovery_rebinds_and_revokes_everything(client,tick,method):
    c=client;old_secret,codes=login(c);old_cookie=c.cookies.get(auth.COOKIE)
    tick()
    proof={'password':PASSWORD} if method=='password' else {'code':otp(old_secret)} if method=='totp' else {'second_recovery_code':codes[1]}
    r=recover(c,codes,**proof);assert r.status_code==200
    new_secret=r.json()['manual_key'];assert new_secret!=old_secret
    assert c.get('/api/accounts').status_code==401
    c.cookies.set(auth.COOKIE,old_cookie)
    assert c.get('/api/accounts').status_code==401
    c.cookies.delete(auth.COOKIE)
    assert recover(c,codes,**proof).status_code==401
    assert password_stage(c).status_code==401
    r=c.post('/api/auth/enroll/confirm',json={'code':otp(new_secret)})
    assert r.status_code==200 and set(r.json()['recovery_codes']).isdisjoint(codes)
    assert c.get('/api/accounts').json()[0]['id']=='test'
    c.post('/api/logout');tick()
    assert password_stage(c,NEW_PASSWORD).status_code==200
    assert verify(c,old_secret).status_code==401
    assert verify(c,new_secret).status_code==200

def test_recovery_requires_joint_proof_and_attempt_limit(client):
    c=client;_,codes=login(c);c.post('/api/logout')
    assert c.post('/api/auth/recover',json={'username':'owner','new_password':NEW_PASSWORD}).status_code==422
    assert recover(c,codes).status_code==401
    assert recover(c,codes,second_recovery_code=codes[0]).status_code==401
    assert recover(c,codes,password='wrong').status_code==401
    assert recover(c,['invalid'],password=PASSWORD).status_code==401
    assert recover(c,['invalid'],password=PASSWORD).status_code==401
    assert recover(c,codes,password=PASSWORD).status_code==429

def test_expired_and_exhausted_challenges(client,tick):
    c=client;secret,_=login(c);c.post('/api/logout');tick()
    password_stage(c)
    wrong='000000' if otp(secret)!='000000' else '111111'
    for _ in range(5):assert c.post('/api/auth/login/verify',json={'code':wrong}).status_code==401
    assert verify(c,secret).status_code==401
    password_stage(c)
    with store.connection() as db:db.execute('UPDATE challenges SET expires_at=0')
    assert verify(c,secret).status_code==401

def test_enrollment_expiry_and_attempt_limit(client):
    c=client
    r=c.post('/api/auth/enroll/start',json={'username':'owner','password':PASSWORD})
    secret=r.json()['manual_key'];wrong='000000' if otp(secret)!='000000' else '111111'
    for _ in range(5):assert c.post('/api/auth/enroll/confirm',json={'code':wrong}).status_code==400
    assert c.post('/api/auth/enroll/confirm',json={'code':otp(secret)}).status_code==400
    r=c.post('/api/auth/enroll/start',json={'username':'owner','password':PASSWORD})
    with store.connection() as db:db.execute('UPDATE enrollments SET expires_at=0')
    assert c.post('/api/auth/enroll/confirm',json={'code':otp(r.json()['manual_key'])}).status_code==400

@pytest.mark.parametrize('column',['expires_at','last_seen'])
def test_session_expiration(client,column):
    c=client;login(c)
    with store.connection() as db:db.execute(f'UPDATE sessions SET {column}=0')
    assert c.get('/api/accounts').status_code==401

def test_code_rotation_requires_fresh_proof(client,tick):
    c=client;secret,codes=login(c)
    body={'password':PASSWORD,'code':otp(secret)}
    assert c.post('/api/auth/recovery-codes/rotate',json=body).status_code==401
    tick();body['code']=otp(secret)
    r=c.post('/api/auth/recovery-codes/rotate',json=body);assert r.status_code==200
    assert set(r.json()['recovery_codes']).isdisjoint(codes)
    assert recover(c,codes,password=PASSWORD).status_code==401

def test_local_mode_requires_proof_and_can_be_protected(client,tick):
    c=client
    assert c.post('/api/auth/mode',json={'mode':'local'}).status_code==200
    assert c.get('/api/accounts').status_code==200
    assert c.get('/api/auth').json()['mode']=='local'
    assert c.post('/api/auth/mode',json={'mode':'account'}).status_code==409
    secret,_=register(c,intent='local')
    assert c.get('/api/auth').json()['mode']=='account'
    c.post('/api/logout')
    assert c.post('/api/auth/mode',json={'mode':'local'}).status_code==403
    assert c.get('/api/accounts').status_code==401
    tick();password_stage(c);assert verify(c,secret).status_code==200
    assert c.post('/api/auth/mode',json={'mode':'local','password':PASSWORD,'code':otp(secret)}).status_code==401
    tick();assert c.post('/api/auth/mode',json={'mode':'local','password':PASSWORD,'code':otp(secret)}).status_code==200
    assert c.get('/api/accounts').status_code==200
    tick();assert c.post('/api/auth/mode',json={'mode':'account','password':PASSWORD,'code':otp(secret)}).status_code==200
    assert c.get('/api/accounts').status_code==401

def test_nonlocal_forwarded_origin_and_obsolete_routes(client):
    c=client;login(c)
    assert c.get('/api/accounts',headers={'X-Forwarded-For':'127.0.0.1'}).status_code==403
    assert c.post('/api/logout',headers={'Origin':'https://evil.invalid'}).status_code==403
    with TestClient(app,client=('192.168.1.10',50000)) as remote:
        assert remote.get('/api/auth').status_code==403
    for path in ['setup','code','register','reset','bind']:
        assert c.post('/api/auth/'+path,json={}).status_code==404
    assert c.post('/api/auth/login',json={'username':'bad name','password':'wrong'}).status_code==422

def test_users_cannot_read_or_change_another_portfolio(client):
    c=client;login(c);bid=upload(c,[deposit(),row()])
    assert c.post(f'/api/batches/{bid}/commit',json={'version':1}).status_code==200
    rid=c.post('/api/reports/all').json()['id']
    c.post('/api/logout');register(c,username='other')
    assert c.get('/api/dashboard/all').json()['event_count']==0
    assert c.get('/api/dashboard/all').json()['ledger_version']==0
    for path in ['/api/accounts','/api/batches','/api/audit']:assert c.get(path).json()==[]
    for path in ['/api/dashboard/test',f'/api/batches/{bid}/source',f'/api/reports/{rid}/download','/api/research/sol-history']:assert c.get(path).status_code==404
    assert c.post(f'/api/batches/{bid}/retract',json={'version':2}).status_code==404
    assert c.put(f'/api/batches/{bid}',json={'version':1,'rows':[]}).status_code==404
    r=c.post('/api/accounts',json={'name':'new','platform':'moomoo','currency':'USD'})
    assert r.status_code==200
    assert c.get('/api/dashboard/'+r.json()['id']).status_code==200

def test_migration_preserves_owner_and_requires_old_password(client):
    from app.auth_migration import migrate
    salt='01'*16
    encoded=hashlib.scrypt(PASSWORD.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
    with Session.begin() as s:s.get(Setting,'auth').value={'hash':encoded,'salt':salt}
    with store.connection() as db:db.execute("DELETE FROM metadata WHERE key='v03_imported'")
    migrate();migrate()
    c=client;assert c.get('/api/auth').json()['legacy_available']
    assert c.post('/api/auth/mode',json={'mode':'local'}).status_code==401
    assert c.post('/api/auth/enroll/start',json={'username':'owner','password':NEW_PASSWORD}).status_code==409
    body={'username':'new-owner','password':NEW_PASSWORD,'intent':'legacy','legacy_username':'owner','legacy_password':'wrong'}
    assert c.post('/api/auth/enroll/start',json=body).status_code==401
    register(c,username='new-owner',password=NEW_PASSWORD,intent='legacy',legacy_username='owner',legacy_password=PASSWORD)
    assert c.get('/api/accounts').json()[0]['id']=='test'
    with store.connection() as db:
        assert db.execute('SELECT id FROM users').fetchone()[0]=='legacy'
        r=db.execute('SELECT * FROM legacy_accounts').fetchone();assert r['claimed']==1 and r['credential']==''

def test_dpapi_and_missing_key_fail_closed(client):
    secret='disposable-test-value'
    assert store.decrypt(store.encrypt(secret))==secret
    login(client)
    keypath=store.AUTH_DIR/'masterkey.dpapi'
    protected=keypath.read_bytes();assert not protected.startswith(b'gAAAA')
    keypath.unlink()
    try:
        with pytest.raises(RuntimeError,match='密钥文件缺失'):store.cipher()
    finally:keypath.write_bytes(protected)
