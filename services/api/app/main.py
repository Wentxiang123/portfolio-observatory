import hashlib, secrets, json, csv, io
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D, localcontext
from urllib.request import urlopen, Request as URLRequest
from fastapi import FastAPI, Request, Response, HTTPException, UploadFile, File, Form, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select
from .db import Session, Setting, Account, Batch, Event, Quote, Job, Audit, SavedReport, now, uid
from .config import PRIVATE, ROOT
from .domain import FIELDS, validate, compute, number
from .auth import router as auth_router, require_user, identity, local_request
from .db import request_owner, ledger_key
from .db import Instrument
from .market import router as market_router, quote_status
from starlette.concurrency import run_in_threadpool
import os
from contextlib import asynccontextmanager
from sqlalchemy.dialects.postgresql import insert

@asynccontextmanager
async def lifespan(app):
    from .auth_migration import migrate
    await run_in_threadpool(migrate)
    yield

app=FastAPI(title='持仓观测站',version='0.4.0',lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware,allowed_hosts=os.environ.get('ALLOWED_HOSTS','127.0.0.1,localhost,testserver').split(','))
app.include_router(auth_router)
app.include_router(market_router)

@app.middleware('http')
async def local_only(request, call_next):
    if not local_request(request):return Response('Localhost access only',status_code=403)
    origin=request.headers.get('origin')
    if request.method not in {'GET','HEAD','OPTIONS'} and origin and origin not in set(os.environ.get('ALLOWED_ORIGINS','http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8000,http://localhost:8000').split(',')):
        return Response('Origin rejected',status_code=403)
    owner=await run_in_threadpool(identity,request)
    context=request_owner.set(owner or '__anonymous__')
    try: result=await call_next(request)
    finally: request_owner.reset(context)
    result.headers['X-Content-Type-Options']='nosniff';result.headers['Cache-Control']='no-store'
    result.headers['X-Frame-Options']='DENY';result.headers['Referrer-Policy']='same-origin'
    return result

@app.exception_handler(ValueError)
async def validation_error(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail':str(exc)},status_code=422)

@app.get('/api/health')
def health():
    with Session() as s:s.execute(select(Setting.key).limit(1))
    return {'status':'ok','version':'0.4.0','database':'postgresql','authentication':'local SQLite'}

protected=[Depends(require_user)]
def ledger(s,lock=False):
    key=ledger_key()
    s.execute(insert(Setting).values(key=key,value={'n':0}).on_conflict_do_nothing(index_elements=['key']))
    query=select(Setting).where(Setting.key==key)
    return s.scalar(query.with_for_update() if lock else query)
def account(s,aid):
    a=s.get(Account,aid)
    if not a:raise HTTPException(404,'账户不存在。')
    return a

def event_dict(e):
    return {k:e.occurred_at.astimezone(timezone.utc).isoformat() if k=='occurred_at' else str(getattr(e,k)) for k in FIELDS}|{'evidence':e.evidence,'batch_id':e.batch_id}

def all_rows(s,aid):return [event_dict(e) for e in s.scalars(select(Event).where(Event.account_id==aid,Event.active==1))]

def batch_dict(b):return {'id':b.id,'account_id':b.account_id,'filename':b.filename,'status':b.status,'version':b.version,'created_at':b.created_at.isoformat(),'payload':b.payload}

def snapshot(s,aid):
    if aid=='all':return portfolio_snapshot(s)
    a=account(s,aid);rows=all_rows(s,aid);quotes={}
    latest_event=max((datetime.fromisoformat(r['occurred_at']) for r in rows),default=None)
    instruments={i.symbol:i for i in s.scalars(select(Instrument).where(Instrument.account_id==aid))}
    for q in s.scalars(select(Quote).where(Quote.currency==a.currency).order_by(Quote.as_of.desc(),Quote.extra['retrieved_at'].as_string().desc().nulls_last())):
        instrument=instruments.get(q.symbol)
        if q.extra.get('retired_mapping'):continue
        if not q.instrument_id and instrument and instrument.mapping_revision:continue
        if q.instrument_id and (not instrument or q.instrument_id!=instrument.id):continue
        if q.symbol in quotes:continue
        quotes[q.symbol]={'price':str(q.price),'as_of':q.as_of.isoformat(),'source':q.source,'id':q.id,**q.extra,
                          **(quote_status(instrument,q) if instrument else {'delay_label':'已存参考价格','stale':True})}
    result=compute(rows,quotes,a.currency)
    result['ledger_version']=ledger(s).value['n']
    result['account']={'id':a.id,'name':a.name,'platform':a.platform,'currency':a.currency,'confirmed_at':a.confirmed_at}
    result['events']=sorted(rows,key=lambda r:r['occurred_at'],reverse=True)
    result['warnings']+=['统计范围为当前子账户；账户间划转不代表全平台新增投入。','尚无完整净值序列：TWR、最大回撤、夏普、年化波动率暂不计算。']
    if any(q.get('stale') for q in quotes.values()):result['warnings'].append('部分估值行情滞后或未关联自动来源，请关注价格时间与数据状态。')
    with localcontext() as ctx:
        ctx.prec=192
        total=D(result['known_value']);positions=result['positions']
        result['concentration']=str(max((D(p['value'])/total for p in positions if p['value'] is not None),default=D(0))) if total>0 and result['complete_valuation'] else None
        reason={'title':'收益从哪里来','market_appreciation':None,'trade_fee_drag':None,'change_24h':None,'opening_pnl':None,'notes':[]}
        if result['contributions'] and not result['sell_count'] and result['complete_valuation']:
            contribution=sum((D(x['pnl']) for x in result['contributions']),D(0))
            tradefees=sum((D(r['fee'])*D(r['price']) if r['fee_currency']==r['symbol'] else D(r['fee']) for r in rows if r['kind']=='BUY'),D(0))
            reason.update(market_appreciation=str(contribution+tradefees),trade_fee_drag=str(tradefees))
            reason['notes'].append('价格贡献按净持仓数量 ×（估值价－成交价）计算；再减买入手续费的成交时价值，与持仓浮盈勾稽。')
        if len(positions)==1:
            p=positions[0];q=p['quote']
            if q and q.get('open24h') and latest_event and latest_event<=datetime.fromisoformat(q['as_of'])-timedelta(hours=24):
                confirmed=datetime.fromisoformat(a.confirmed_at) if a.confirmed_at else None
                if confirmed and confirmed>=datetime.fromisoformat(q['as_of']):
                    change=D(p['quantity'])*(D(q['price'])-D(q['open24h']))
                    reason.update(change_24h=str(change),opening_pnl=str(D(p['pnl'])-change))
                    reason['notes'].append('24小时变化以该行情时点为终点，并依据已确认的持仓未变；不代表今天的收益。')
        reason['notes'].append('新闻只提供背景与可能解释；时间上的相关性不等于因果，不分配未经验证的“新闻贡献率”。')
        result['attribution']=reason
        asset_value=sum((D(p['value']) for p in positions if p['value'] is not None),D(0))
        result['scenarios']=[{'shock':x,'change':str(asset_value*D(x)/100),'nav':str(total+asset_value*D(x)/100)} for x in [-30,-20,-10,10,20,30]] if result['complete_valuation'] else []
    return result

def portfolio_snapshot(s):
    """One personal portfolio; source ledgers retain independent cash/cost histories."""
    sources=[snapshot(s,a.id) for a in s.scalars(select(Account).order_by(Account.platform,Account.id))]
    active=[d for d in sources if d['event_count']]
    with localcontext() as ctx:
        ctx.prec=192
        def combine(items,currency):
            result={}
            for key in ['cash','known_value','cost','realized','income','fees','net_invested','unrealized','total_pnl']:
                result[key]=str(sum((D(d[key]) for d in items),D(0))) if all(d[key] is not None for d in items) else None
            result['complete_valuation']=all(d['complete_valuation'] for d in items)
            for key in ['event_count','buy_count','sell_count']:result[key]=sum(d[key] for d in items)
            for key in ['positions','events','contributions']:
                result[key]=[{**r,'platform':d['account']['platform'],'source_name':d['account']['name'],'source_account_id':d['account']['id'],'currency':currency} for d in items for r in d[key]]
            result['events'].sort(key=lambda r:r['occurred_at'],reverse=True)
            result['currency']=currency
            result['cost_return']=str(D(result['unrealized'])/D(result['cost'])) if result['unrealized'] is not None and D(result['cost']) else None
            result['concentration']=None  # Different venues may use the same ticker for different instruments.
            result['warnings']=list(dict.fromkeys(n for d in items for n in d['warnings'] if '子账户' not in n))
            reason={k:(str(sum((D(d['attribution'][k]) for d in items),D(0))) if items and all(d['attribution'][k] is not None for d in items) else None) for k in ['market_appreciation','trade_fee_drag','change_24h','opening_pnl']}
            # 24h windows from different price timestamps must not be added together.
            if len(items)!=1:reason['change_24h']=None;reason['opening_pnl']=None
            reason['notes']=list(dict.fromkeys(n for d in items for n in d['attribution']['notes']))
            result['attribution']=reason
            asset_value=sum((D(p['value']) for p in result['positions'] if p['value'] is not None),D(0))
            result['scenarios']=[{'shock':x,'change':str(asset_value*D(x)/100),'nav':str(D(result['known_value'])+asset_value*D(x)/100)} for x in [-30,-20,-10,10,20,30]] if result['complete_valuation'] else []
            return result
        currencies=sorted({d['currency'] for d in active})
        groups=[combine([d for d in active if d['currency']==currency],currency) for currency in currencies]
        if len(groups)==1:result=dict(groups[0])
        else:
            result={'currency':'多币种','complete_valuation':all(g['complete_valuation'] for g in groups),
              **{key:None for key in ['cash','known_value','cost','realized','income','fees','net_invested','unrealized','total_pnl','cost_return','concentration']},
              **{key:sum(d[key] for d in active) for key in ['event_count','buy_count','sell_count']},
              **{key:[r for g in groups for r in g[key]] for key in ['positions','events','contributions']},
              'attribution':{'market_appreciation':None,'trade_fee_drag':None,'change_24h':None,'opening_pnl':None,'notes':['多币种按计价币分别归因，不将 USD、USDT、CNY 相加。']},'scenarios':[],
              'warnings':list(dict.fromkeys(n for g in groups for n in g['warnings']))}
        result['events'].sort(key=lambda r:r['occurred_at'],reverse=True)
        result.update(account={'id':'all','name':'我的投资账户','platform':'ALL','currency':result['currency'],'confirmed_at':None},
          ledger_version=ledger(s).value['n'],groups=groups,sources=sources,multi_currency=len(groups)>1)
        result['warnings']+=['所有平台属于同一个投资账户；保留来源用于凭证、费用与余额核对。','不同币种分栏展示；暂未折算全账户总资产或集中度。平台之间转账不能视为新增投入。']
        for d in active:
            if d['concentration'] is not None and D(d['concentration'])>D('.5'):
                result['warnings'].append(f"{d['account']['platform']} / {d['currency']} 来源的单一资产占比 {D(d['concentration'])*100:.2f}%，存在集中持仓风险；不是全账户占比。")
        return result

@app.get('/api/accounts',dependencies=protected)
def accounts():
    with Session() as s:return [{'id':a.id,'name':a.name,'platform':a.platform,'currency':a.currency,'confirmed_at':a.confirmed_at} for a in s.scalars(select(Account).order_by(Account.name))]

class NewAccount(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    platform:str=Field(pattern='^(OKX|moomoo|Alipay|Other)$')
    currency:str=Field(pattern='^[A-Z]{2,10}$')

@app.post('/api/accounts',dependencies=protected)
def add_account(body:NewAccount):
    with Session.begin() as s:
        a=Account(**body.model_dump());s.add(a);s.flush();return {'id':a.id}

@app.get('/api/dashboard/{aid}',dependencies=protected)
def dashboard(aid:str):
    with Session() as s:return snapshot(s,aid)

@app.get('/api/batches',dependencies=protected)
def batches(aid:str='all'):
    with Session() as s:
        query=select(Batch)
        if aid!='all':account(s,aid);query=query.where(Batch.account_id==aid)
        result=[]
        for b in s.scalars(query.order_by(Batch.created_at.desc())):
            a=account(s,b.account_id)
            result.append({**batch_dict(b),'platform':a.platform,'currency':a.currency,'source_name':a.name})
        return result

@app.post('/api/import-source',dependencies=protected)
async def upload_source(file:UploadFile=File(...),platform:str=Form(...),currency:str=Form(...)):
    # UI asks only which platform a statement came from, never asks to create accounts.
    import re
    if platform not in {'OKX','moomoo','Alipay','Other'} or not re.fullmatch('[A-Z]{2,10}',currency):
        raise ValueError('请选择有效的平台来源与计价币。')
    with Session.begin() as s:
        ledger(s,True)
        matches=list(s.scalars(select(Account).where(Account.platform==platform,Account.currency==currency).order_by(Account.id)))
        canonical={'OKX':'okx','moomoo':'moomoo','Alipay':'alipay'}.get(platform)
        a=next((a for a in matches if a.id==canonical),matches[0] if matches else None)
        if a is None:
            a=Account(name=f'{platform} · {currency}',platform=platform,currency=currency);s.add(a);s.flush()
        aid=a.id
    return await upload(aid,file)

@app.post('/api/import/{aid}',dependencies=protected)
async def upload(aid:str,file:UploadFile=File(...)):
    from pathlib import Path
    suffix=Path(file.filename or '').suffix.lower()
    if suffix not in {'.csv','.xlsx','.pdf','.png','.jpg','.jpeg','.webp'}:raise HTTPException(422,'请选择CSV/XLSX/PDF/PNG/JPG/WebP文件；ZIP请先解压。')
    blob=await file.read(20*1024*1024+1)
    if not blob or len(blob)>20*1024*1024:raise HTTPException(422,'文件须为1字节至20MB。')
    digest=hashlib.sha256(blob).hexdigest()
    with Session.begin() as s:
        account(s,aid)
        ledger(s,True)
        existing=s.scalar(select(Batch).where(Batch.account_id==aid,Batch.sha256==digest))
        if existing:return batch_dict(existing)
        key=uid()+suffix;folder=PRIVATE/'uploads';folder.mkdir(exist_ok=True);(folder/key).write_bytes(blob)
        b=Batch(account_id=aid,filename=Path(file.filename).name[:255],sha256=digest,storage_key=key)
        s.add(b);s.flush();s.add(Job(batch_id=b.id));return batch_dict(b)

def get_batch(s,bid,lock=False):
    query=select(Batch).where(Batch.id==bid)
    if lock:query=query.with_for_update()
    b=s.scalar(query)
    if not b:raise HTTPException(404,'批次不存在。')
    return b

@app.get('/api/batches/{bid}/source',dependencies=protected)
def source(bid:str):
    with Session() as s:
        b=get_batch(s,bid);return FileResponse(PRIVATE/'uploads'/b.storage_key,filename=b.filename,content_disposition_type='inline')

class Review(BaseModel):
    version:int
    rows:list[dict]=Field(max_length=10000)

@app.put('/api/batches/{bid}',dependencies=protected)
def review(bid:str,body:Review):
    with Session.begin() as s:
        b=get_batch(s,bid,True)
        if b.status not in {'review','failed'} or b.version!=body.version:raise HTTPException(409,'批次状态或版本已变化，请刷新。')
        a=account(s,b.account_id)
        original={r.get('source_id'):r.get('evidence',{}) for r in b.payload.get('rows',[])}
        rows=[validate({**r,'evidence':original.get(r.get('source_id'),{'manual':True})},a.currency) for r in body.rows]
        b.payload={**b.payload,'rows':rows,'issues':[]};b.version+=1;b.status='review'
        s.add(Audit(action='review',target=bid,details={'version':b.version,'rows':rows}))
        return batch_dict(b)

def candidate(s,b):
    a=account(s,b.account_id)
    rows=[validate(r,a.currency) for r in b.payload.get('rows',[])]
    if not rows:raise ValueError('请先补充有效流水。')
    if b.payload.get('issues'):raise ValueError('请修正解析问题并保存核对结果。')
    existing={e.source_id:e for e in s.scalars(select(Event).where(Event.account_id==a.id))}
    new=[];seen=set();duplicates=0
    for r in rows:
        key=r['source_id']
        if key in seen:raise ValueError('同一批次记录ID重复，请先核对。')
        seen.add(key)
        if key in existing:
            e=existing[key];old=event_dict(e)
            identical=all(D(old[k])==D(r[k]) if k in {'quantity','price','fee'} else old[k]==r[k] for k in FIELDS)
            if not identical or not e.active:raise ValueError(f'记录 {key} 与既有记录冲突或已被撤销，请人工核对来源ID。')
            duplicates+=1
        else:new.append(r)
    combined=all_rows(s,a.id)+new
    result=compute(combined,{},a.currency)
    if b.payload.get('closing_balances'):
        cutoff=datetime.fromisoformat(b.payload['closing_at'])
        at_cutoff=compute([r for r in combined if datetime.fromisoformat(r['occurred_at'])<=cutoff],{},a.currency)
        quantities={p['symbol']:D(p['quantity']) for p in at_cutoff['positions']}
        quantities[a.currency]=D(at_cutoff['cash'])
        for symbol,expected in b.payload['closing_balances'].items():
            if abs(quantities.get(symbol,D(0))-D(expected))>D('1e-8'):
                raise ValueError(f'{symbol} 重建余额与原 PDF 期末余额不符，请补齐期初或遗漏流水后入账。')
    return new,{'new_count':len(new),'duplicate_count':duplicates,'cash':result['cash'],'positions':result['positions']}

@app.post('/api/batches/{bid}/preview',dependencies=protected)
def preview(bid:str):
    with Session() as s:
        b=get_batch(s,bid)
        if b.status!='review':raise HTTPException(409,'请等待解析完成后再核对。')
        return candidate(s,b)[1]

class Version(BaseModel):version:int

@app.post('/api/batches/{bid}/commit',dependencies=protected)
def commit(bid:str,body:Version):
    with Session.begin() as s:
        ledger_state=ledger(s,True)
        b=get_batch(s,bid,True)
        if b.status=='committed':return {'ok':True,'already_committed':True}
        if b.status!='review' or b.version!=body.version:raise HTTPException(409,'状态或版本已变化，请重新预览。')
        rows,info=candidate(s,b)
        for r in rows:
            s.add(Event(account_id=b.account_id,batch_id=b.id,**{**r,'occurred_at':datetime.fromisoformat(r['occurred_at'])}))
        b.status='committed';b.version+=1;ledger_state.value={'n':ledger_state.value['n']+1}
        s.add(Audit(action='commit',target=bid,details=info));return {'ok':True,**info}

@app.post('/api/batches/{bid}/retract',dependencies=protected)
def retract(bid:str,body:Version):
    with Session.begin() as s:
        ledger_state=ledger(s,True);b=get_batch(s,bid,True)
        if b.status!='committed' or b.version!=body.version:raise HTTPException(409,'请刷新批次后重试。')
        rows=[r for r in all_rows(s,b.account_id) if r['batch_id']!=bid]
        compute(rows,{},account(s,b.account_id).currency)
        for e in s.scalars(select(Event).where(Event.batch_id==bid)):e.active=0
        b.status='retracted';b.version+=1;ledger_state.value={'n':ledger_state.value['n']+1}
        s.add(Audit(action='retract',target=bid,details={'version':b.version}));return {'ok':True}

class Price(BaseModel):
    account_id:str|None=None
    symbol:str=Field(pattern='^[A-Z0-9._:/-]{1,40}$')
    currency:str=Field(pattern='^[A-Z]{2,10}$')
    price:str
    as_of:datetime
    source:str=Field(min_length=1,max_length=140)

@app.post('/api/quotes',dependencies=protected)
def add_quote(body:Price):
    p=number(body.price)
    if p<=0 or not body.as_of.tzinfo or body.as_of>now()+timedelta(minutes=5):raise ValueError('价格须大于0，时间须带时区且不能在未来。')
    with Session.begin() as s:
        query=select(Instrument).where(Instrument.symbol==body.symbol,Instrument.currency==body.currency)
        if body.account_id:
            target=account(s,body.account_id)
            if target.currency!=body.currency:raise ValueError('价格计价币与所选来源不一致。')
            query=query.where(Instrument.account_id==body.account_id)
        matches=list(s.scalars(query))
        if len(matches)>1:raise ValueError('同一代码关联了多个来源，请选择价格所属的平台来源。')
        if body.account_id and not matches:
            if not any(p['symbol']==body.symbol for p in compute(all_rows(s,body.account_id),{},body.currency)['positions']):
                raise ValueError('所选来源中没有该资产的持仓。')
            instrument=Instrument(account_id=body.account_id,symbol=body.symbol,currency=body.currency,
                                  asset_type='stock',market='',provider_symbol=body.symbol,enabled=0)
            s.add(instrument);s.flush();matches=[instrument]
        q=Quote(**{**body.model_dump(exclude={'account_id'}),'price':p,'source':'手工：'+body.source},
                instrument_id=matches[0].id if matches else None,extra={'delay_label':'手工参考价格','retrieved_at':now().isoformat()});s.add(q)
        s.add(Audit(action='quote',target=body.symbol,details={'source':body.source,'as_of':body.as_of.isoformat()}))
    return {'ok':True}

@app.post('/api/quotes/okx/{aid}',dependencies=protected)
def refresh_okx(aid:str):
    with Session() as s:
        if aid=='all':
            positions=[p for d in portfolio_snapshot(s)['sources'] if d['account']['platform']=='OKX' and d['currency']=='USDT' for p in d['positions']]
            positions=list({p['symbol']:p for p in positions}.values())
        else:
            a=account(s,aid)
            if a.platform!='OKX' or a.currency!='USDT':raise ValueError('公共行情接口当前仅支持OKX的USDT现货来源。')
            positions=snapshot(s,aid)['positions']
    if not positions:raise ValueError('请先导入持仓。')
    updated=[]
    try:
        for p in positions[:20]:
            symbol=p['symbol']
            import re
            if not re.fullmatch('[A-Z0-9]{1,20}',symbol):continue
            url='https://www.okx.com/api/v5/market/ticker?instId='+symbol+'-USDT'
            with urlopen(URLRequest(url,headers={'User-Agent':'PortfolioLocal/0.1'}),timeout=8) as r:data=json.load(r)
            if data.get('code')!='0' or not data.get('data'):raise ValueError('Invalid ticker')
            ticker=data['data'][0];price=number(ticker['last']);opened=number(ticker['open24h']);as_of=datetime.fromtimestamp(int(ticker['ts'])/1000,timezone.utc)
            if price<=0 or opened<=0 or abs((now()-as_of).total_seconds())>3600:raise ValueError('Stale ticker')
            updated.append(Quote(symbol=symbol,currency='USDT',price=price,as_of=as_of,source='OKX 公共现货接口',extra={'open24h':str(opened)}))
    except Exception:raise HTTPException(502,'公共行情暂时不可用；保留原行情。可稍后重试，或手工填写带时间的价格。')
    with Session.begin() as s:s.add_all(updated)
    return {'updated':len(updated)}

@app.post('/api/accounts/{aid}/confirm',dependencies=protected)
def confirm_holdings(aid:str):
    with Session.begin() as s:
        targets=list(s.scalars(select(Account))) if aid=='all' else [account(s,aid)]
        for a in targets:a.confirmed_at=now().isoformat()
        s.add(Audit(action='confirm_holdings',target=aid,details={'sources':[a.id for a in targets]}))
    return {'ok':True}

@app.get('/api/template',dependencies=protected)
def template(currency:str='USDT'):
    import re
    if not re.fullmatch('[A-Z]{2,10}',currency):raise ValueError('计价币格式不正确。')
    symbol='SOL' if currency=='USDT' else 'AAPL' if currency=='USD' else 'FUND_CODE'
    f=io.StringIO();w=csv.writer(f);w.writerow(FIELDS)
    w.writerow(['example-deposit','2026-10-14T09:00:00+08:00','DEPOSIT',currency,currency,'100','1','0',currency])
    w.writerow(['example-buy','2026-10-14T10:00:00+08:00','BUY',symbol,currency,'0.5','100','0',currency])
    return Response('\ufeff'+f.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=portfolio-template.csv'})

def historical_research():
    if request_owner.get() not in {None,'legacy'}:return None
    path=ROOT/'output/reports/SOL持仓与交易分析_20261021.md'
    if not path.exists():return None
    content=path.read_text(encoding='utf-8')
    section=content.split('### 3.3 ',1)
    if len(section)!=2:return None
    return {'as_of':'2026-10-21T01:49:51+08:00','sha256':hashlib.sha256(content.encode()).hexdigest(),'text':'### 3.3 '+section[1].split('## 4.',1)[0]}

@app.get('/api/research/sol-history',dependencies=protected)
def research():
    data=historical_research()
    if not data:raise HTTPException(404,'本机暂无这份历史研究报告。')
    return Response('# 历史研究材料 · 非最新消息\n\n适用行情：'+data['as_of']+'\n\n'+data['text'],media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':'attachment; filename=SOL-research-20261021.md'})

@app.post('/api/reports/{aid}',dependencies=protected)
def report(aid:str):
    with Session.begin() as s:
        d=snapshot(s,aid);a=d['attribution'];stamp=now().isoformat()
        history=historical_research() if any(p['symbol']=='SOL' for p in d['positions']) else None
        lines=[f"# {d['account']['name']} 持仓报告",f'生成时间：{stamp}',f"账本版本：{d['ledger_version']}；计价币：{d['currency']}",
          f"已定价资产与现金：{d['known_value']}；估值完整：{d['complete_valuation']}",f"持仓成本：{d['cost']}；浮动盈亏：{d['unrealized']}；已实现盈亏：{d['realized']}",
          '## 盈利与亏损原因',f"持仓价格贡献：{a['market_appreciation']}；买入费用拖累：{a['trade_fee_drag']}",
          f"参考行情前24小时变化：{a['change_24h']}；窗口起点浮盈：{a['opening_pnl']}",*a['notes'],'## 数据范围与限制',*d['warnings'],
          '行情与持仓的完整快照附后，null表示条件不足；不等于0。']
        if aid=='all':
            lines=[f"# 我的投资账户 · 全平台报告",f"生成时间：{stamp}；账本版本：{d['ledger_version']}",'不同币种分别展示，未作外汇折算；所有平台属于同一投资账户。']
            for g in d['groups']:
                lines += [f"## {g['currency']}",f"已定价资产：{g['known_value']}；现金：{g['cash']}；估值完整：{g['complete_valuation']}",f"浮盈：{g['unrealized']}；已实现：{g['realized']}；持仓成本：{g['cost']}",
                  '### 盈利与亏损原因',f"价格贡献：{g['attribution']['market_appreciation']}；买入手续费拖累：{g['attribution']['trade_fee_drag']}",*g['attribution']['notes']]
            lines+=['## 数据范围与限制',*d['warnings']]
        if history:
            lines+=['## 历史市场背景附件（非当前行情归因）',f"以下材料来自本机既有报告，只对应 {history['as_of']} 的历史窗口；未在本次生成报告时重新核验新闻。来源文件 SHA-256：{history['sha256']}",history['text']]
        lines+=['## 冻结数据快照','```json',json.dumps(d,ensure_ascii=False,indent=2),'```']
        r=SavedReport(account_id=aid,ledger_version=d['ledger_version'],content='\n\n'.join(lines));s.add(r);s.flush();return {'id':r.id}

@app.get('/api/reports/{rid}/download',dependencies=protected)
def download_report(rid:str):
    with Session() as s:
        r=s.get(SavedReport,rid)
        if not r:raise HTTPException(404)
        return Response(r.content,media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':f'attachment; filename=portfolio-{rid[:8]}.md'})

@app.get('/api/audit',dependencies=protected)
def audit():
    with Session() as s:return [{'action':a.action,'target':a.target,'created_at':a.created_at.isoformat()} for a in s.scalars(select(Audit).order_by(Audit.created_at.desc()).limit(50))]
