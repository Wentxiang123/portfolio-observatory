import json
from datetime import datetime, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app import auth, market
from app.db import Session, User, Quote, Event, Instrument, EmailChallenge, AuthThrottle, now
from test_api import client, login, upload, register
from test_domain import deposit, row

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

def test_mapping_change_retires_old_quotes_and_other_user_cannot_change_it(client,monkeypatch):
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
    c.post('/api/logout');register(c,username='other')
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
