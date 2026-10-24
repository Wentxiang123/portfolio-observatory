import json
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app import auth, market
from app.db import Session, User, Quote, Event, Instrument, EmailChallenge, AuthThrottle, now
from test_api import client, login, upload
from test_domain import deposit, row

@pytest.fixture
def mail(monkeypatch):
    sent=[]
    monkeypatch.setattr(auth,'smtp_ready',lambda:True)
    monkeypatch.setattr(auth,'send_code',lambda email,code,purpose:sent.append((email,code,purpose)))
    return sent

def code(c,mail,email,purpose='register'):
    r=c.post('/api/auth/code',json={'email':email,'purpose':purpose})
    assert r.status_code==200,r.text
    return mail[-1][1]

def register(c,mail,email='one@example.test',username='one'):
    otp=code(c,mail,email)
    r=c.post('/api/auth/register',json={'email':email,'username':username,'password':'Private-password-123','code':otp})
    assert r.status_code==200,r.text
    return otp

def clear_mail_throttle():
    with Session.begin() as s:
        for r in s.scalars(select(AuthThrottle)):s.delete(r)

def test_registration_private_portfolios_and_objects(client,mail):
    c=client;login(c);bid=upload(c,[deposit(),row()])
    assert c.post(f'/api/batches/{bid}/commit',json={'version':1}).status_code==200
    rid=c.post('/api/reports/all').json()['id']
    c.post('/api/logout');register(c,mail)
    assert c.get('/api/dashboard/all').json()['event_count']==0
    assert c.get('/api/dashboard/all').json()['ledger_version']==0
    assert c.get('/api/accounts').json()==[] and c.get('/api/batches').json()==[]
    assert c.get('/api/audit').json()==[]
    for path in ['/api/dashboard/test',f'/api/batches/{bid}/source',f'/api/reports/{rid}/download','/api/research/sol-history']:
        assert c.get(path).status_code==404
    for path,body in [(f'/api/batches/{bid}/retract',{'version':2}),('/api/accounts/test/confirm',{}),(f'/api/batches/{bid}/commit',{'version':1})]:
        assert c.post(path,json=body).status_code==404
    assert c.put(f'/api/batches/{bid}',json={'version':1,'rows':[]}).status_code==404
    assert c.post('/api/auth/bind',json={'email':'steal@example.test','username':'steal','password':'Private-password-123','code':'000000'}).status_code==403
    aid=c.post('/api/accounts',json={'name':'new','platform':'moomoo','currency':'USD'}).json()['id']
    c.post('/api/quotes',json={'symbol':'AAA','currency':'USD','price':'999','as_of':now().isoformat(),'source':'private'})
    c.post('/api/logout');c.post('/api/auth/login',json={'password':'Local-Test-Only-9384'})
    assert c.get('/api/dashboard/test').json()['positions'][0]['quote'] is None
    assert c.get('/api/dashboard/'+aid).status_code==404

def test_reset_is_single_use_and_revokes_sessions(client,mail):
    c=client;register(c,mail)
    old_cookie=c.cookies.get('portfolio_session');clear_mail_throttle()
    otp=code(c,mail,'one@example.test','reset')
    body={'email':'one@example.test','password':'New-private-password-456','code':otp}
    assert c.post('/api/auth/reset',json=body).status_code==200
    assert c.get('/api/accounts').status_code==401
    assert c.post('/api/auth/reset',json=body).status_code==400
    assert c.post('/api/auth/login',json={'email':'one@example.test','password':'Private-password-123'}).status_code==401
    assert c.post('/api/auth/login',json={'email':'one@example.test','password':'New-private-password-456'}).status_code==200
    assert c.cookies.get('portfolio_session')!=old_cookie

def test_code_attempt_expiry_and_resend_limits(client,mail):
    c=client;otp=code(c,mail,'new@example.test')
    assert c.post('/api/auth/code',json={'email':'new@example.test','purpose':'register'}).status_code==429
    wrong='000000' if otp!='000000' else '111111'
    body={'email':'new@example.test','username':'new','password':'Private-password-123','code':wrong}
    for _ in range(5):assert c.post('/api/auth/register',json=body).status_code==400
    body['code']=otp;assert c.post('/api/auth/register',json=body).status_code==400
    key='register:'+auth.digest('new@example.test')
    with Session() as s:
        challenge=s.get(EmailChallenge,key)
        assert challenge.attempts==5 and challenge.code_hash!=otp
    clear_mail_throttle();otp=code(c,mail,'new@example.test')
    with Session.begin() as s:s.get(EmailChallenge,key).expires_at=now()-timedelta(seconds=1)
    body['code']=otp;assert c.post('/api/auth/register',json=body).status_code==400

def test_binding_preserves_legacy_and_disables_old_password(client,mail):
    c=client;login(c);old=c.cookies.get('portfolio_session')
    assert c.get('/api/auth').json()['needs_email_binding']
    otp=code(c,mail,'owner@example.test','bind')
    assert c.post('/api/auth/bind',json={'email':'owner@example.test','username':'owner','password':'Private-password-123','code':otp}).status_code==200
    assert c.get('/api/accounts').json()[0]['id']=='test'
    c.cookies.clear();c.cookies.set('portfolio_session',old)
    assert c.get('/api/accounts').status_code==401
    assert c.post('/api/auth/login',json={'password':'Local-Test-Only-9384'}).status_code==409
    assert c.post('/api/auth/login',json={'email':'owner@example.test','password':'Private-password-123'}).status_code==200

def test_smtp_missing_does_not_claim_delivery(client,monkeypatch):
    monkeypatch.setattr(auth,'smtp_ready',lambda:False)
    assert client.post('/api/auth/code',json={'email':'a@example.test','purpose':'reset'}).status_code==503
    assert not client.get('/api/auth').json()['email_configured']

def test_smtp_tls_transport_and_safe_error(monkeypatch):
    for key,value in {'SMTP_HOST':'smtp.example.test','SMTP_FROM':'sender@example.test','SMTP_USERNAME':'sender','SMTP_PASSWORD':'local-only-secret','SMTP_SECURITY':'starttls','SMTP_PORT':'587'}.items():monkeypatch.setenv(key,value)
    actions=[]
    class SMTP:
        def __init__(self,*args,**kwargs):actions.append('connect')
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def starttls(self,**kwargs):actions.append('tls')
        def login(self,*args):actions.append('login')
        def send_message(self,message):
            actions.append('send');assert message['To']=='recipient@example.test' and '123456' in message.get_content()
    monkeypatch.setattr(auth.smtplib,'SMTP',SMTP)
    auth.send_code('recipient@example.test','123456','reset')
    assert actions==['connect','tls','login','send']
    def fail(*args,**kwargs):raise OSError('local-only-secret')
    monkeypatch.setattr(auth.smtplib,'SMTP',fail)
    with pytest.raises(auth.HTTPException) as error:auth.send_code('recipient@example.test','123456','reset')
    assert error.value.status_code==503 and 'local-only-secret' not in error.value.detail

def test_unknown_reset_uniform_response_and_no_mail(client,mail):
    register(client,mail);clear_mail_throttle()
    yes=client.post('/api/auth/code',json={'email':'one@example.test','purpose':'reset'})
    count=len(mail)
    no=client.post('/api/auth/code',json={'email':'missing@example.test','purpose':'reset'})
    assert yes.json()==no.json() and len(mail)==count

def test_stock_calendar_dst_holiday_lunch_and_crypto():
    us=SimpleNamespace(asset_type='stock',market='US')
    assert market.market_state(us,datetime.fromisoformat('2026-10-23T13:30:00+00:00'))=='交易中'
    assert market.market_state(us,datetime.fromisoformat('2026-11-02T13:30:00+00:00'))!='交易中'
    assert market.market_state(us,datetime.fromisoformat('2026-11-02T14:30:00+00:00'))=='交易中'
    assert market.market_state(us,datetime.fromisoformat('2026-12-25T16:00:00+00:00'))!='交易中'
    hk=SimpleNamespace(asset_type='etf',market='HK')
    assert market.market_state(hk,datetime.fromisoformat('2026-10-23T04:30:00+00:00'))!='交易中'
    assert market.next_poll(hk,datetime.fromisoformat('2026-10-23T04:30:00+00:00')).hour==5
    crypto=SimpleNamespace(asset_type='crypto',market='CRYPTO')
    assert market.market_state(crypto,datetime.fromisoformat('2026-10-25T12:00:00+00:00'))=='全天交易'

def test_official_daily_nav_only_and_missing_nav_refused(monkeypatch):
    i=SimpleNamespace(asset_type='fund',provider_symbol='000001',currency='CNY',market='FUND_CN')
    payload='var fS_code="000001";var Data_netWorthTrend=[{"x":1792598400000,"y":1.2345}];var estimate=9.99;'
    monkeypatch.setattr(market,'read_text',lambda *a:payload)
    q=market.fetch_quote(i)
    assert str(q['price'])=='1.2345' and q['extra']['time_precision']=='date'
    assert q['extra']['nav_date']=='2026-10-22'
    monkeypatch.setattr(market,'read_text',lambda *a:'var fS_code="000001";var Data_millionCopiesIncome=[1];')
    with pytest.raises(ValueError):market.fetch_quote(i)

def test_quote_worker_does_not_change_transactions_and_failure_preserves_price(client,monkeypatch):
    c=client;login(c)
    from app.db import Account
    with Session.begin() as s:s.get(Account,'test').platform='moomoo'
    bid=upload(c,[deposit(),row()]);c.post(f'/api/batches/{bid}/commit',json={'version':1})
    market.discover()
    items=c.get('/api/instruments').json();iid=items[0]['id']
    monkeypatch.setattr(market,'fetch_quote',lambda i:dict(price=number('110'),as_of=now(),source='test',extra={'delay_label':'延迟未知'}))
    assert market.run_quote_once()
    before=c.get('/api/dashboard/test').json()
    assert float(before['positions'][0]['quote']['price'])==110
    assert before['positions'][0]['quote']['delay_label']=='延迟未知'
    with Session.begin() as s:s.get(Instrument,iid).next_poll=now()-timedelta(seconds=1)
    def fail(i):raise TimeoutError('secret must not be stored')
    monkeypatch.setattr(market,'fetch_quote',fail)
    market.run_quote_once()
    after=c.get('/api/dashboard/test').json()
    assert after['events']==before['events'] and after['positions'][0]['quantity']==before['positions'][0]['quantity']
    assert after['positions'][0]['quote']['price']==before['positions'][0]['quote']['price']
    assert 'secret' not in c.get('/api/instruments').text
    c.post('/api/logout')
    assert c.put('/api/instruments/'+iid,json={}).status_code==401

def test_mapping_change_retires_old_quotes_and_other_user_cannot_change_it(client,mail,monkeypatch):
    c=client;login(c)
    from app.db import Account
    with Session.begin() as s:s.get(Account,'test').platform='moomoo'
    bid=upload(c,[deposit(),row()]);c.post(f'/api/batches/{bid}/commit',json={'version':1})
    market.discover();iid=c.get('/api/instruments').json()[0]['id']
    stamp=now()
    monkeypatch.setattr(market,'fetch_quote',lambda i:dict(price=number('110'),as_of=stamp,source='test',extra={}))
    market.run_quote_once()
    monkeypatch.setattr(market,'fetch_quote',lambda i:dict(price=number('120'),as_of=stamp,source='corrected',extra={}))
    with Session.begin() as s:s.get(Instrument,iid).next_poll=now()-timedelta(seconds=1)
    market.run_quote_once()
    assert float(c.get('/api/dashboard/test').json()['positions'][0]['quote']['price'])==120
    mapping={'asset_type':'etf','market':'US','provider_symbol':'AAA','enabled':True}
    assert c.put('/api/instruments/'+iid,json=mapping).status_code==200
    assert c.get('/api/dashboard/test').json()['positions'][0]['quote'] is None
    market.run_quote_once()
    assert c.get('/api/dashboard/test').json()['positions'][0]['quote'] is not None
    c.post('/api/logout');register(c,mail)
    assert c.get('/api/instruments').json()==[]
    assert c.put('/api/instruments/'+iid,json=mapping).status_code==404

def test_closed_weekend_quote_not_stale_but_old_session_is():
    i=SimpleNamespace(asset_type='stock',market='US',error=None)
    sunday=datetime.fromisoformat('2026-10-25T12:00:00+00:00')
    q=SimpleNamespace(as_of=datetime.fromisoformat('2026-10-23T20:00:00+00:00'),extra={})
    assert not market.quote_status(i,q,sunday)['stale']
    q.as_of-=timedelta(days=1)
    assert market.quote_status(i,q,sunday)['stale']

def test_stock_provider_rejects_wrong_currency_and_timezone(monkeypatch):
    i=SimpleNamespace(asset_type='stock',provider_symbol='AAPL',currency='USD',market='US')
    meta={'symbol':'AAPL','currency':'HKD','exchangeTimezoneName':'America/New_York','instrumentType':'EQUITY','regularMarketPrice':200,'regularMarketTime':now().timestamp()}
    monkeypatch.setattr(market,'read_text',lambda *a:json.dumps({'chart':{'result':[{'meta':meta}]}}))
    with pytest.raises(ValueError):market.fetch_quote(i)
    meta['currency']='USD';meta['exchangeTimezoneName']='Asia/Hong_Kong'
    with pytest.raises(ValueError):market.fetch_quote(i)
    meta['exchangeTimezoneName']='America/New_York'
    assert '延迟未知' in market.fetch_quote(i)['extra']['delay_label']

from app.domain import number
