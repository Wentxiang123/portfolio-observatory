from decimal import Decimal as D
from pathlib import Path
import pytest
from app.domain import validate,compute
from app.adapters import parse_file

def row(kind='BUY',q='1',p='100',fee='0',fee_currency='USD',id='b',time='2026-10-01T10:00:00+08:00',symbol='AAA'):
    return validate(dict(source_id=id,occurred_at=time,kind=kind,symbol=symbol,currency='USD',quantity=q,price=p,fee=fee,fee_currency=fee_currency))

def deposit(q='1000'):
    return row('DEPOSIT',q,'1',id='a',symbol='USD',time='2026-10-01T09:00:00+08:00')

def test_base_fee_cost_and_cash():
    result=compute([deposit(),row(fee='.01',fee_currency='AAA')],{'AAA':{'price':'110'}},'USD')
    assert D(result['cash'])==900
    assert D(result['positions'][0]['quantity'])==D('.99')
    assert D(result['unrealized'])==D('8.9')
    assert D(result['total_pnl'])==D('8.9')

def test_quote_fee_and_partial_sale():
    result=compute([deposit(),row(q='2',fee='1'),row('SELL',q='1',p='120',fee='2',id='c',time='2026-10-02T10:00:00+08:00')],{'AAA':{'price':'110'}},'USD')
    assert D(result['realized'])==D('17.5')
    assert D(result['unrealized'])==D('9.5')
    assert D(result['total_pnl'])==27
    assert not result['contributions']

def test_sell_base_fee_full_close():
    result=compute([deposit(),row(q='1.01'),row('SELL',fee='.01',fee_currency='AAA',p='120',id='c',time='2026-10-02T10:00:00+08:00')],{},'USD')
    assert not result['positions']
    assert D(result['total_pnl'])==19

def test_dividend_standalone_fees_withdrawal():
    rows=[deposit(),row('DIVIDEND','10','1',id='b',symbol='USD'),row('FEE','2','1',id='c',symbol='USD'),row('WITHDRAWAL','100','1',id='d',symbol='USD')]
    result=compute(rows,{},'USD')
    assert D(result['total_pnl'])==8
    assert D(result['net_invested'])==900

def test_missing_price_not_zero_and_zero_cost_forbidden():
    result=compute([deposit(),row()],{},'USD')
    assert result['unrealized'] is None and not result['complete_valuation']
    with pytest.raises(ValueError):row('OPENING',p='0')

def test_incomplete_history_rejected():
    with pytest.raises(ValueError):compute([row()],{},'USD')
    with pytest.raises(ValueError):compute([deposit(),row('SELL')],{},'USD')

@pytest.mark.parametrize('field,value',[('fee',''),('quantity','NaN'),('price','-1'),('quantity','1e-25'),('occurred_at','2026-01-01T10:00:00'),('fee_currency','BNB')])
def test_invalid_values(field,value):
    r=row(fee='1');r[field]=value
    with pytest.raises((ValueError,TypeError)):validate(r,'USD')

def test_standard_csv_and_xlsx(tmp_path):
    from app.domain import FIELDS
    import csv
    source=[deposit(),row()];path=tmp_path/'test.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,FIELDS,extrasaction='ignore');w.writeheader();w.writerows(source)
    parsed=parse_file(path,'USD');assert len(parsed['rows'])==2
    from openpyxl import Workbook
    wb=Workbook();ws=wb.active;ws.append(FIELDS)
    for r in source:ws.append([r[k] for k in FIELDS])
    path=tmp_path/'test.xlsx';wb.save(path)
    assert len(parse_file(path,'USD')['rows'])==2
