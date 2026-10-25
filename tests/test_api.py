import uuid, csv, io
import pytest
from sqlalchemy import text, select
from fastapi.testclient import TestClient
from app.db import engine, Base, Session, Setting, Account, Quote, Batch, Job
from app.main import app
from app.worker import run_once
from app.domain import FIELDS
from test_domain import deposit,row

@pytest.fixture
def client(tmp_path,monkeypatch):
    from app import auth_store
    monkeypatch.setattr(auth_store,"AUTH_DIR",tmp_path/"auth")
    auth_store.initialize()
    with auth_store.connection() as db:db.execute("INSERT INTO metadata VALUES ('v03_imported','yes')")
    # Each test owns an isolated schema. No production rows are changed or truncated.
    schema='test_'+uuid.uuid4().hex
    with engine.begin() as c:c.execute(text(f'CREATE SCHEMA {schema}'))
    isolated=engine.execution_options(schema_translate_map={None:schema})
    Base.metadata.create_all(isolated);Session.configure(bind=isolated)
    with Session.begin() as s:
        s.add_all([Setting(key='ledger_version',value={'n':0}),Setting(key='auth',value={}),Account(id='test',name='test',platform='Other',currency='USD')])
    try:
        with TestClient(app,client=('127.0.0.1',50000)) as c:yield c
    finally:
        Session.configure(bind=engine)
        Base.metadata.drop_all(isolated)
        with engine.begin() as c:c.execute(text(f'DROP SCHEMA {schema}'))

def register(c,username='owner',password='Local-Test-Only-9384',**extra):
    import pyotp
    from app import auth
    r=c.post('/api/auth/enroll/start',json={'username':username,'password':password,**extra})
    assert r.status_code==200, r.status_code
    secret=r.json()['manual_key']
    r=c.post('/api/auth/enroll/confirm',json={'code':pyotp.TOTP(secret).at(auth.clock())})
    assert r.status_code==200, r.status_code
    return secret,r.json()['recovery_codes']

def login(c):return register(c)
def upload(c,rows,filename='test.csv'):
    f=io.StringIO();w=csv.DictWriter(f,FIELDS,extrasaction='ignore');w.writeheader();w.writerows(rows)
    r=c.post('/api/import/test',files={'file':(filename,f.getvalue(),'text/csv')});assert r.status_code==200,r.text
    run_once();return r.json()['id']

def test_auth_and_origin(client):
    c=client
    assert c.get('/api/accounts').status_code==401
    assert c.post('/api/auth/setup',json={'password':'abcdabcd'},headers={'Origin':'https://evil.invalid'}).status_code==403
    login(c);assert c.get('/api/accounts').status_code==200
    assert c.post('/api/auth/setup',json={'password':'abcdabcd'}).status_code==404
    assert c.post('/api/logout').status_code==200
    assert c.get('/api/accounts').status_code==401
    assert c.post('/api/auth/login',json={'username':'owner','password':'Local-Test-Only-9384'}).status_code==200

def test_review_atomic_commit_duplicate_and_report(client):
    c=client;login(c);bid=upload(c,[deposit(),row()])
    assert c.get('/api/dashboard/test').json()['event_count']==0
    assert c.post(f'/api/batches/{bid}/preview').json()['new_count']==2
    assert c.post(f'/api/batches/{bid}/commit',json={'version':99}).status_code==409
    assert c.post(f'/api/batches/{bid}/commit',json={'version':1}).status_code==200
    assert c.post(f'/api/batches/{bid}/commit',json={'version':1}).json()['already_committed']
    assert upload(c,[deposit(),row()])==bid
    assert c.post('/api/quotes',json={'symbol':'AAA','currency':'USD','price':'110','as_of':'2026-10-20T12:00:00+08:00','source':'test'}).status_code==200
    d=c.get('/api/dashboard/test').json();assert d['event_count']==2 and float(d['unrealized'])==10
    r=c.post('/api/reports/test').json();content=c.get(f"/api/reports/{r['id']}/download").text
    assert '盈利与亏损原因' in content and '冻结数据快照' in content
    assert c.post(f'/api/batches/{bid}/retract',json={'version':2}).status_code==200
    assert c.get('/api/dashboard/test').json()['event_count']==0
    assert c.get(f"/api/reports/{r['id']}/download").text==content

def test_failed_ledger_does_not_partially_post(client):
    c=client;login(c);bid=upload(c,[row()])
    assert c.post(f'/api/batches/{bid}/commit',json={'version':1}).status_code==422
    assert c.get('/api/dashboard/test').json()['event_count']==0
    assert c.get('/api/batches?aid=test').json()[0]['status']=='review'

def test_source_conflict_and_retraction_dependencies(client):
    c=client;login(c);first=upload(c,[deposit()])
    assert c.post(f'/api/batches/{first}/commit',json={'version':1}).status_code==200
    second=upload(c,[row()]);assert c.post(f'/api/batches/{second}/commit',json={'version':1}).status_code==200
    assert c.post(f'/api/batches/{first}/retract',json={'version':2}).status_code==422
    conflict=upload(c,[row(p='101')]);assert c.post(f'/api/batches/{conflict}/preview').status_code==422
    assert c.get('/api/dashboard/test').json()['event_count']==2

def test_invalid_review_preserves_version_and_worker_recovery(client):
    c=client;login(c);bid=upload(c,[deposit(),row()])
    bad=row();bad['fee']=''
    assert c.put(f'/api/batches/{bid}',json={'version':1,'rows':[bad]}).status_code==422
    assert c.get('/api/batches?aid=test').json()[0]['version']==1
    from app.db import now
    from datetime import timedelta
    with Session.begin() as s:
        job=s.scalar(select(Job).where(Job.batch_id==bid));job.status='running';job.lease_until=now()-timedelta(seconds=1)
        s.get(Batch,bid).status='parsing'
    assert run_once()
    assert c.get('/api/batches?aid=test').json()[0]['status']=='review'

def test_public_quote_success_failure_and_stale_rejection(client,monkeypatch):
    import json
    from app import main
    from app.db import now
    c=client;login(c)
    with Session.begin() as s:
        a=s.get(Account,'test');a.platform='OKX';a.currency='USDT'
    rows=[deposit(),row()]
    for r in rows:
        r['currency']='USDT';r['fee_currency']='USDT'
        if r['symbol']=='USD':r['symbol']='USDT'
    bid=upload(c,rows);assert c.post(f'/api/batches/{bid}/commit',json={'version':1}).status_code==200
    def response(ts):return io.BytesIO(json.dumps({'code':'0','data':[{'last':'110','open24h':'105','ts':str(ts)}]}).encode())
    monkeypatch.setattr(main,'urlopen',lambda *a,**k:response(int(now().timestamp()*1000)))
    assert c.post('/api/quotes/okx/test').json()['updated']==1
    before=c.get('/api/dashboard/test').json()['positions'][0]['quote']
    monkeypatch.setattr(main,'urlopen',lambda *a,**k:response(0))
    assert c.post('/api/quotes/okx/test').status_code==502
    assert c.get('/api/dashboard/test').json()['positions'][0]['quote']==before
    def fail(*args,**kwargs):raise TimeoutError()
    monkeypatch.setattr(main,'urlopen',fail)
    assert c.post('/api/quotes/okx/test').status_code==502
    assert c.get('/api/dashboard/test').json()['positions'][0]['quote']==before

def test_quote_time_and_auth_lockout(client):
    c=client;login(c)
    assert c.post('/api/quotes',json={'symbol':'AAA','currency':'USD','price':'100','as_of':'2026-01-01T12:00:00','source':'test'}).status_code==422
    assert c.post('/api/quotes',json={'symbol':'AAA','currency':'USD','price':'NaN','as_of':'2026-01-01T12:00:00Z','source':'test'}).status_code==422
    c.post('/api/logout')
    for _ in range(5):assert c.post('/api/auth/login',json={'username':'owner','password':'Wrong-Password'}).status_code==401
    assert c.post('/api/auth/login',json={'username':'owner','password':'Local-Test-Only-9384'}).status_code==429

def test_statement_balance_control_blocks_missing_opening(client):
    c=client;login(c);bid=upload(c,[deposit(),row()])
    with Session.begin() as s:
        b=s.get(Batch,bid)
        b.payload={**b.payload,'closing_balances':{'USD':'900','AAA':'2'},'closing_at':'2026-10-01T10:00:00+08:00'}
    response=c.post(f'/api/batches/{bid}/commit',json={'version':1})
    assert response.status_code==422 and '期末余额' in response.json()['detail']
    assert c.get('/api/dashboard/test').json()['event_count']==0

def source_upload(c,platform,currency,quantity='1'):
    rows=[deposit(),row(q=quantity)]
    for r in rows:
        if r['symbol']=='USD':r['symbol']=currency
        r['currency']=currency;r['fee_currency']=currency
    f=io.StringIO();w=csv.DictWriter(f,FIELDS,extrasaction='ignore');w.writeheader();w.writerows(rows)
    response=c.post('/api/import-source',data={'platform':platform,'currency':currency},files={'file':('statement.csv',f.getvalue(),'text/csv')})
    assert response.status_code==200,response.text
    bid=response.json()['id'];run_once()
    assert c.post(f'/api/batches/{bid}/commit',json={'version':1}).status_code==200
    return bid

def test_unified_account_multicurrency_import_and_frozen_report(client):
    c=client;login(c)
    for platform,currency in [('Alipay','CNY'),('moomoo','USD'),('OKX','USDT')]:
        source_upload(c,platform,currency)
        assert c.post('/api/quotes',json={'symbol':'AAA','currency':currency,'price':'110','as_of':'2026-10-20T12:00:00Z','source':'fixture'}).status_code==200
    d=c.get('/api/dashboard/all').json()
    assert d['account']['id']=='all' and d['multi_currency']
    assert d['known_value'] is None and d['unrealized'] is None
    assert d['event_count']==6 and len(d['positions'])==3
    assert {g['currency'] for g in d['groups']}=={'CNY','USD','USDT'}
    assert all(float(g['known_value'])==1010 and float(g['unrealized'])==10 for g in d['groups'])
    assert {p['platform'] for p in d['positions']}=={'Alipay','moomoo','OKX'}
    assert len(c.get('/api/batches').json())==3
    before=d['event_count'];source_upload(c,'moomoo','USD');assert c.get('/api/dashboard/all').json()['event_count']==before
    rid=c.post('/api/reports/all').json()['id'];content=c.get(f'/api/reports/{rid}/download').text
    assert all(x in content for x in ['全平台报告','## CNY','## USD','## USDT','盈利与亏损原因'])
    assert c.post('/api/accounts/all/confirm').status_code==200

def test_unified_same_currency_totals_and_platform_id_collision(client):
    c=client;login(c)
    source_upload(c,'moomoo','USD');source_upload(c,'Other','USD','2')
    c.post('/api/quotes',json={'symbol':'AAA','currency':'USD','price':'110','as_of':'2026-10-20T12:00:00Z','source':'fixture'})
    d=c.get('/api/dashboard/all').json()
    assert not d['multi_currency'] and d['currency']=='USD'
    assert float(d['known_value'])==2030 and float(d['unrealized'])==30 and float(d['cost'])==300
    assert d['event_count']==4 and len(d['positions'])==2
    assert len({r['source_account_id'] for r in d['events']})==2
    assert d['attribution']['change_24h'] is None

def test_source_reuses_existing_account_and_currency_template(client):
    c=client;login(c)
    with Session.begin() as s:s.add(Account(id='moomoo',name='Existing moomoo',platform='moomoo',currency='USD'))
    bid=source_upload(c,'moomoo','USD')
    with Session() as s:assert s.get(Batch,bid).account_id=='moomoo'
    assert len([a for a in c.get('/api/accounts').json() if a['platform']=='moomoo'])==1
    template=c.get('/api/template?currency=CNY').text
    assert 'CNY' in template and 'USDT' not in template
    assert c.get('/api/template?currency=BAD-CODE').status_code==422
