from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime, timezone
import re

D = Decimal
ZERO = D('0')
KINDS = {'BUY','SELL','DEPOSIT','WITHDRAWAL','OPENING','DIVIDEND','FEE'}
FIELDS = ['source_id','occurred_at','kind','symbol','currency','quantity','price','fee','fee_currency']

def number(value):
    try:
        n=D(str(value))
        if not n.is_finite() or n.as_tuple().exponent < -24 or n.adjusted() >= 36:
            raise ValueError()
        return n
    except (InvalidOperation, ValueError):
        raise ValueError('数量或金额须为有限十进制数，最多24位小数。')

def validate(row, currency=None):
    r={k:str(row.get(k,'')).strip() for k in FIELDS}
    if any(not r[k] for k in FIELDS):
        raise ValueError('必填字段不完整；手续费为0也须明确填写，未知费用不能当作0。')
    r['kind']=r['kind'].upper(); r['symbol']=r['symbol'].upper()
    r['currency']=r['currency'].upper(); r['fee_currency']=r['fee_currency'].upper()
    if r['kind'] not in KINDS:
        raise ValueError('当前支持买卖、出入金、已知成本期初余额、收入与费用。')
    if r['kind'] in {'BUY','SELL'} and r['symbol']==r['currency']:
        raise ValueError('交易资产不能与账户计价币相同。')
    if len(r['source_id'])>200 or not re.fullmatch(r'[A-Z0-9._:/-]{1,40}',r['symbol']):
        raise ValueError('资产代码或记录ID格式不正确。')
    if currency and r['currency']!=currency:
        raise ValueError('流水计价币与所选账单计价币不一致，请按币种拆分文件并选择对应计价币，无需创建账户。')
    t=datetime.fromisoformat(r['occurred_at'].replace('Z','+00:00'))
    if t.tzinfo is None:
        raise ValueError('成交时间须包含时区，例如 2026-10-14T22:49:21+08:00。')
    r['occurred_at']=t.astimezone(timezone.utc).isoformat()
    q,p,f=(number(r[k]) for k in ('quantity','price','fee'))
    if q<=0 or p<0 or f<0 or (r['kind'] in {'BUY','SELL'} and p<=0):
        raise ValueError('数量须大于0，成交价须大于0，费用不得为负。')
    if r['fee_currency'] not in {r['symbol'],r['currency']} and f:
        raise ValueError('第三币种扣费尚待适配，不能在本版自动入账。')
    if r['kind']=='BUY' and r['fee_currency']==r['symbol'] and f>=q:
        raise ValueError('手续费不得超过取得数量。')
    if r['kind'] in {'DEPOSIT','WITHDRAWAL','DIVIDEND','FEE'}:
        if r['symbol']!=r['currency'] or f:
            raise ValueError('资金/收入/费用记录须使用账户报价币，费用请独立成行。')
    if r['kind']=='OPENING' and f:
        raise ValueError('期初数量与单位历史成本须单独确认，不另带费用。')
    if r['kind']=='OPENING' and r['symbol']!=r['currency'] and p<=0:
        raise ValueError('本版资产期初记录须提供已知正数单位成本；未知成本不能填0。')
    for k,n in zip(('quantity','price','fee'),(q,p,f)): r[k]=str(n)
    r['evidence']=row.get('evidence',{})
    return r

def compute(rows, quotes, currency):
    with localcontext() as ctx:
        ctx.prec=192
        return _compute(rows,quotes,currency)

def _compute(rows,quotes,currency):
    balances={}; cash=ZERO; deposits=ZERO; withdrawn=ZERO; income=ZERO; fees=ZERO; realized=ZERO
    lots=[]; warnings=[]
    rows=sorted(rows,key=lambda r:(datetime.fromisoformat(r['occurred_at']),r['source_id']))
    for r in rows:
        q,p,f=(D(r[k]) for k in ('quantity','price','fee'))
        sym=r['symbol']; kind=r['kind']; fee_currency=r['fee_currency']
        if kind in {'DEPOSIT','DIVIDEND'} or (kind=='OPENING' and sym==currency):
            cash+=q
            if kind=='DIVIDEND': income+=q
            else: deposits+=q
        elif kind in {'WITHDRAWAL','FEE'}:
            cash-=q
            if kind=='WITHDRAWAL': withdrawn+=q
            else: fees+=q
        else:
            b=balances.setdefault(sym,{'quantity':ZERO,'cost':ZERO})
            if kind in {'BUY','OPENING'}:
                net=q-(f if fee_currency==sym else ZERO)
                cost=q*p+(f if fee_currency==currency else ZERO)
                b['quantity']+=net; b['cost']+=cost
                if kind=='BUY': cash-=cost
                else: deposits+=cost
                fees+=f*p if fee_currency==sym else f
                lots.append({**r,'net_quantity':str(net),'cost':str(cost)})
            elif kind=='SELL':
                removed=q+(f if fee_currency==sym else ZERO)
                if removed>b['quantity']: raise ValueError(f'{sym} 卖出数量超出已知持仓，请补齐期初或历史交易。')
                allocated=b['cost'] if removed==b['quantity'] else b['cost']*removed/b['quantity']
                proceeds=q*p-(f if fee_currency==currency else ZERO)
                cash+=proceeds; realized+=proceeds-allocated
                b['quantity']-=removed; b['cost']-=allocated
                fees+=f*p if fee_currency==sym else f
        if cash < ZERO:
            raise ValueError('现金余额为负，可能缺少期初资金或划转，请补齐后入账。')
    positions=[]; known_value=cash; total_cost=ZERO; total_pnl=ZERO; all_priced=True
    for sym,b in balances.items():
        if b['quantity']==0: continue
        quote=quotes.get(sym)
        value=b['quantity']*D(quote['price']) if quote else None
        pnl=value-b['cost'] if value is not None else None
        if value is not None: known_value+=value;total_pnl+=pnl
        else: all_priced=False;warnings.append(f'{sym} 缺少估值价格')
        total_cost+=b['cost']
        positions.append({'symbol':sym,'quantity':str(b['quantity']),'cost':str(b['cost']),
          'average_cost':str(b['cost']/b['quantity']),'value':str(value) if value is not None else None,
          'pnl':str(pnl) if pnl is not None else None,'return':str(pnl/b['cost']) if pnl is not None and b['cost'] else None,'quote':quote})
    # Fees are already embedded in trade P&L; standalone fees are tracked separately.
    standalone_fees=sum((D(r['quantity']) for r in rows if r['kind']=='FEE'),ZERO)
    economic=known_value-deposits+withdrawn if all_priced else None
    if economic is not None and abs(economic-(total_pnl+realized+income-standalone_fees))>D('1e-20'):
        raise ValueError('损益勾稽失败，不能发布结果。')
    no_sells=not any(r['kind']=='SELL' for r in rows)
    contributions=[]
    if no_sells:
        for lot in lots:
            quote=quotes.get(lot['symbol'])
            if quote:
                pnl=D(lot['net_quantity'])*D(quote['price'])-D(lot['cost'])
                contributions.append({**lot,'pnl':str(pnl)})
    return {'currency':currency,'cash':str(cash),'known_value':str(known_value),'complete_valuation':all_priced,
      'cost':str(total_cost),'unrealized':str(total_pnl) if all_priced else None,'realized':str(realized),
      'income':str(income),'fees':str(fees),'net_invested':str(deposits-withdrawn),
      'total_pnl':str(economic) if economic is not None else None,
      'cost_return':str(total_pnl/total_cost) if all_priced and total_cost else None,
      'positions':positions,'contributions':contributions,'event_count':len(rows),
      'buy_count':sum(r['kind']=='BUY' for r in rows),'sell_count':sum(r['kind']=='SELL' for r in rows),
      'warnings':warnings,'latest_event_at':rows[-1]['occurred_at'] if rows else None}

