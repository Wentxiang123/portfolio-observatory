"""Read-only pricing. Never creates or modifies ledger events or quantities."""
import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import quote as urlquote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import pandas as pd
import exchange_calendars as xcals
from sqlalchemy import select
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from .auth import require_user, throttle
from .db import Account, Event, Quote, Instrument, Setting, Session, request_owner, now, uid
from .domain import compute, FIELDS, number

router = APIRouter(dependencies=[Depends(require_user)])
MARKETS = {'US':('XNYS','America/New_York','USD'), 'HK':('XHKG','Asia/Hong_Kong','HKD'),
           'CN_SH':('XSHG','Asia/Shanghai','CNY'), 'CN_SZ':('XSHG','Asia/Shanghai','CNY'),
           'JP':('XTKS','Asia/Tokyo','JPY'), 'GB':('XLON','Europe/London','GBP')}

@lru_cache(maxsize=8)
def calendar(market): return xcals.get_calendar(MARKETS[market][0])

def market_state(i, stamp=None):
    stamp=stamp or now()
    if i.asset_type == 'crypto': return '全天交易'
    if i.asset_type == 'fund': return '日终净值 · 等待基金公司公布'
    if i.market not in MARKETS: return '待配置市场'
    try: return '交易中' if calendar(i.market).is_open_on_minute(pd.Timestamp(stamp).floor('min')) else '休市 / 午间休市'
    except (ValueError, IndexError): return '交易日历超出覆盖范围'

def next_poll(i, stamp):
    if i.asset_type == 'crypto': return stamp+timedelta(seconds=30)
    if i.asset_type == 'fund':
        local=stamp.astimezone(ZoneInfo('Asia/Shanghai'))
        # Daily final NAV only, including late/QDII publication; no intraday estimates.
        return stamp+timedelta(minutes=60 if local.hour >= 16 or local.hour < 2 else 360)
    cal=calendar(i.market); minute=pd.Timestamp(stamp).floor('min')
    if cal.is_open_on_minute(minute): return stamp+timedelta(seconds=60)
    close=cal.previous_close(minute+pd.Timedelta(minutes=1)).to_pydatetime()
    if stamp-close < timedelta(minutes=45): return stamp+timedelta(minutes=5)
    # During lunch use next trading minute; overnight/weekend wake exactly at next session.
    return cal.minute_to_trading_minute(minute, direction='next').to_pydatetime()

def read_text(url, referer=None):
    headers={'User-Agent':'Mozilla/5.0 PortfolioObservatory/0.3','Accept':'application/json,text/javascript,*/*'}
    if referer: headers['Referer']=referer
    with urlopen(Request(url, headers=headers), timeout=10) as response:
        payload=response.read(8*1024*1024+1)
        if len(payload)>8*1024*1024: raise ValueError('行情响应过大')
        return payload.decode('utf-8-sig')

def js_value(text, name):
    match=re.search(r'\bvar\s+'+re.escape(name)+r'\s*=\s*',text)
    if not match: raise ValueError('缺少正式单位净值字段，不能将万份收益当作净值')
    return json.JSONDecoder().raw_decode(text[match.end():])[0]

def fetch_quote(i):
    stamp=now(); symbol=i.provider_symbol
    extra={'asset_type':i.asset_type, 'market':i.market, 'retrieved_at':stamp.isoformat()}
    if i.asset_type == 'crypto':
        data=json.loads(read_text('https://www.okx.com/api/v5/market/ticker?instId='+urlquote(symbol,safe='-')))
        if data.get('code') != '0' or not data.get('data'): raise ValueError('OKX 没有返回有效现货行情')
        row=data['data'][0]
        if row.get('instId') != symbol: raise ValueError('返回资产与配置不一致')
        price=number(row['last']); as_of=datetime.fromtimestamp(int(row['ts'])/1000,timezone.utc)
        if stamp-as_of>timedelta(minutes=5): raise ValueError('OKX 返回的价格已过期')
        extra.update(open24h=str(number(row['open24h'])),delay_label='公共现货行情 · 30 秒轮询')
        source='OKX 公共现货接口'
    elif i.asset_type == 'fund':
        text=read_text(f'https://fund.eastmoney.com/pingzhongdata/{symbol}.js', 'https://fund.eastmoney.com/')
        if str(js_value(text,'fS_code')) != symbol: raise ValueError('基金代码不匹配')
        series=js_value(text,'Data_netWorthTrend')
        if not series: raise ValueError('尚无已公布单位净值')
        row=max(series,key=lambda r:r['x'])
        nav_date=datetime.fromtimestamp(row['x']/1000,ZoneInfo('Asia/Shanghai')).date()
        if nav_date>stamp.astimezone(ZoneInfo('Asia/Shanghai')).date(): raise ValueError('净值日期在未来')
        price=number(str(row['y']))
        # Convention only; provider supplies valuation date, not publication timestamp.
        as_of=datetime.combine(nav_date,datetime.min.time(),ZoneInfo('Asia/Shanghai')).replace(hour=15).astimezone(timezone.utc)
        extra.update(nav_date=nav_date.isoformat(),time_precision='date',delay_label='已公布日终单位净值 · 非盘中估值')
        source='天天基金 / 东方财富单位净值'
    else:
        data=json.loads(read_text('https://query1.finance.yahoo.com/v8/finance/chart/'+urlquote(symbol,safe='')+'?interval=1m&range=5d&includePrePost=false'))
        meta=data['chart']['result'][0]['meta']
        if meta.get('symbol','').upper()!=symbol.upper(): raise ValueError('行情代码不匹配')
        currency=meta.get('currency'); scale='1'
        if currency=='GBp' and i.currency=='GBP': currency='GBP'; scale='0.01'
        if currency!=i.currency: raise ValueError('行情币种与账单币种不一致')
        if meta.get('exchangeTimezoneName')!=MARKETS[i.market][1]: raise ValueError('行情市场时区与设置不一致')
        if meta.get('instrumentType') not in {'EQUITY','ETF'}: raise ValueError('当前数据不是股票或 ETF')
        price=number(str(meta['regularMarketPrice']))*number(scale)
        as_of=datetime.fromtimestamp(meta['regularMarketTime'],timezone.utc)
        extra.update(delay_label='公开参考行情 · 延迟未知 / 不保证实时',exchange=meta.get('exchangeName'),
                     exchange_timezone=meta.get('exchangeTimezoneName'),asset_type='etf' if meta['instrumentType']=='ETF' else 'stock')
        source='Yahoo Finance 公开参考行情'
    if price<=0 or as_of>stamp+timedelta(minutes=5): raise ValueError('价格或行情时间无效')
    return dict(price=price,as_of=as_of,source=source,extra=extra)

def position_rows(s, a):
    rows=[{k:e.occurred_at.isoformat() if k=='occurred_at' else str(getattr(e,k)) for k in FIELDS}
          for e in s.scalars(select(Event).where(Event.account_id==a.id, Event.active==1))]
    return compute(rows,{},a.currency)['positions']

def discover():
    """Map confirmed user scope: OKX spot, moomoo US, Alipay daily NAV."""
    with Session.begin() as s:
        s.scalar(select(Setting).where(Setting.key=='ledger_version').with_for_update())
        existing={(i.account_id,i.symbol) for i in s.scalars(select(Instrument))}
        for a in s.scalars(select(Account)):
            for p in position_rows(s,a):
                symbol=p['symbol']
                if (a.id,symbol) in existing: continue
                kind='stock';market='';provider=symbol;enabled=0
                if a.platform=='OKX' and re.fullmatch(r'[A-Z0-9]{1,20}',symbol) and a.currency in {'USDT','USDC','USD','BTC','ETH'}:
                    kind='crypto';market='CRYPTO';provider=f'{symbol}-{a.currency}';enabled=1
                elif a.platform=='moomoo' and a.currency=='USD' and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,19}',symbol):
                    market='US';provider=symbol.replace('.','-');enabled=1
                elif a.platform=='Alipay' and a.currency=='CNY' and re.fullmatch(r'\d{6}',symbol):
                    kind='fund';market='FUND_CN';enabled=1
                s.add(Instrument(owner_id=a.owner_id,account_id=a.id,symbol=symbol,currency=a.currency,
                                 asset_type=kind,market=market,provider_symbol=provider,enabled=enabled))
                existing.add((a.id,symbol))

def quote_status(i, q, stamp=None):
    stamp=stamp or now()
    state=market_state(i,stamp)
    if q is None: return {'market_state':state,'stale':True,'freshness':'尚无价格'}
    if i.asset_type=='fund':
        date=q.extra.get('nav_date')
        freshness='净值日期 '+date if date else '手工参考价格'
        if date and date<stamp.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(): freshness+=' · 当日净值尚未公布'
        stale=stamp-q.as_of>timedelta(days=7)
    elif i.asset_type=='crypto':
        stale=stamp-q.as_of>timedelta(minutes=2);freshness='行情可能过期' if stale else '近期行情'
    else:
        try:
            expected=stamp if state=='交易中' else calendar(i.market).previous_close(pd.Timestamp(stamp).floor('min')+pd.Timedelta(minutes=1)).to_pydatetime()
            stale=q.as_of<expected-timedelta(minutes=30)
        except (ValueError,IndexError,KeyError): stale=True
        freshness='行情滞后，注意价格时间' if stale else '最近交易时段价格'
    return {'market_state':state,'stale':stale,'freshness':freshness,'update_error':i.error}

def run_quote_once():
    with Session.begin() as s:
        i=s.scalar(select(Instrument).where(Instrument.enabled==1,Instrument.next_poll<=now()).order_by(Instrument.next_poll).with_for_update(skip_locked=True).limit(1))
        if not i: return False
        token=uid();i.lease_token=token;i.next_poll=now()+timedelta(minutes=2)
        instrument_id=i.id
        a=s.get(Account,i.account_id)
        if not any(p['symbol']==i.symbol for p in position_rows(s,a)):
            i.next_poll=now()+timedelta(hours=6);i.lease_token=None;return True
    data=None;error=None
    try:
        due=next_poll(i,now());data=fetch_quote(i)
    except Exception as exc:
        # Never persist raw exception messages containing URLs/credentials/response bodies.
        error='行情获取失败（'+type(exc).__name__+'），保留上次价格；请核对代码、市场或网络。'
    with Session.begin() as s:
        current=s.scalar(select(Instrument).where(Instrument.id==instrument_id).with_for_update())
        if current.lease_token!=token: return True  # User changed mapping while HTTP was in flight.
        current.checked_at=now();current.lease_token=None
        if error:
            current.error=error;current.failures+=1
            current.next_poll=now()+timedelta(seconds=min(3600,60*2**min(current.failures,6)))
        else:
            current.error=None;current.failures=0;current.next_poll=due
            data['extra']={**data['extra'],'retrieved_at':now().isoformat()}
            last=s.scalar(select(Quote).where(Quote.instrument_id==i.id,Quote.extra['retired_mapping'].as_boolean().is_not(True)).order_by(Quote.as_of.desc(),Quote.extra['retrieved_at'].as_string().desc().nulls_last()).limit(1))
            if last and data['as_of']<last.as_of:
                current.error='数据源返回较旧时间，保留已有较新价格。'
            elif not last or data['as_of']!=last.as_of or data['price']!=last.price:
                s.add(Quote(owner_id=i.owner_id,instrument_id=i.id,symbol=i.symbol,currency=i.currency,**data))
    return True

class Mapping(BaseModel):
    asset_type: str = Field(pattern='^(crypto|stock|etf|fund)$')
    market: str = Field(pattern='^(CRYPTO|FUND_CN|US|HK|CN_SH|CN_SZ|JP|GB)$')
    provider_symbol: str = Field(pattern=r'^[A-Z0-9.^=-]{1,60}$')
    enabled: bool

@router.get('/api/quotes/status')
def worker_status():
    with Session() as s:
        row=s.get(Setting,'quote_worker')
        heartbeat=row.value.get('heartbeat') if row else None
        return {'heartbeat':heartbeat,'running':bool(heartbeat and now()-datetime.fromisoformat(heartbeat)<timedelta(seconds=90))}

@router.get('/api/instruments')
def instruments():
    with Session() as s:
        items=[]
        for i in s.scalars(select(Instrument).order_by(Instrument.symbol)):
            a=s.get(Account,i.account_id)
            q=s.scalar(select(Quote).where(Quote.instrument_id==i.id,Quote.extra['retired_mapping'].as_boolean().is_not(True)).order_by(Quote.as_of.desc(),Quote.extra['retrieved_at'].as_string().desc().nulls_last()).limit(1))
            items.append({k:getattr(i,k) for k in ['id','account_id','symbol','currency','asset_type','market','provider_symbol','enabled','error','checked_at','next_poll']} |
                         {'platform':a.platform,'price':str(q.price) if q else None,'as_of':q.as_of if q else None,
                          'delay_label':q.extra.get('delay_label') if q else None,**quote_status(i,q)})
        return items

@router.put('/api/instruments/{iid}')
def update_mapping(iid: str, body: Mapping):
    with Session.begin() as s:
        i=s.get(Instrument,iid)
        if not i: raise HTTPException(404,'资产不存在。')
        if body.asset_type=='crypto':
            if body.market!='CRYPTO' or not re.fullmatch(r'[A-Z0-9]{1,20}-'+re.escape(i.currency),body.provider_symbol): raise ValueError('现货代码必须匹配计价币，例如 SOL-USDT。')
        elif body.asset_type=='fund':
            if body.market!='FUND_CN' or i.currency!='CNY' or not re.fullmatch(r'\d{6}',body.provider_symbol): raise ValueError('支付宝基金使用六位代码、人民币单位净值。')
        elif body.market not in MARKETS or MARKETS[body.market][2]!=i.currency: raise ValueError('交易所市场与账单币种不匹配。')
        changed=any(getattr(i,k)!=getattr(body,k) for k in ('asset_type','market','provider_symbol'))
        for k,v in body.model_dump().items(): setattr(i,k,int(v) if k=='enabled' else v)
        i.lease_token=None;i.error=None;i.failures=0;i.next_poll=now()
        if changed:
            i.mapping_revision+=1
            # Old mapping prices remain archived, but cannot value the new mapping.
            for q in s.scalars(select(Quote).where(Quote.instrument_id==i.id,Quote.extra['retired_mapping'].as_boolean().is_not(True))):
                q.extra={**q.extra,'retired_mapping':True}
    return {'ok':True}

@router.post('/api/quotes/refresh')
def refresh():
    throttle('quote-refresh:'+request_owner.get(),1,30)
    discover()
    with Session.begin() as s:
        items=list(s.scalars(select(Instrument).where(Instrument.enabled==1)))
        for i in items:
            if not i.lease_token: i.next_poll=now()
    return {'queued':len(items),'message':'已请求后台更新；请保持 Worker 运行。'}
