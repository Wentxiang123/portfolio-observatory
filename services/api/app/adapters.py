import csv, io
from collections import defaultdict
from decimal import Decimal as D
from .domain import FIELDS

def normalize_generic(rows):
    result=[]
    for i,row in enumerate(rows,2):
        r={k:str(row.get(k,'') if row.get(k) is not None else '') for k in FIELDS}
        r['evidence']={'row':i,'raw':{str(k):str(v) for k,v in row.items()}}
        result.append(r)
    return result

def parse_file(path, currency):
    suffix=path.suffix.lower(); notes=[]
    if suffix=='.csv':
        blob=path.read_bytes()
        try: text=blob.decode('utf-8-sig')
        except UnicodeDecodeError: text=blob.decode('gb18030')
        dialect=csv.Sniffer().sniff(text[:8192],delimiters=',;\t')
        rows=list(csv.DictReader(io.StringIO(text),dialect=dialect))
        if len(rows)>10000: raise ValueError('第一版单批最多10,000行。')
        return {'rows':normalize_generic(rows),'notes':['标准列模板；请核对账户、时区和费用。'],'raw_text':text[:16000]}
    if suffix=='.xlsx':
        from openpyxl import load_workbook
        wb=load_workbook(path,read_only=True,data_only=False)
        try:
            sheet=wb.active
            if sheet.max_row>10001: raise ValueError('第一版单批最多10,000行。')
            values=sheet.iter_rows(values_only=True); header=next(values)
            rows=[dict(zip(header,line)) for line in values if any(v is not None for v in line)]
            return {'rows':normalize_generic(rows),'notes':['读取首张工作表；公式不会执行。'],'raw_text':''}
        finally: wb.close()
    if suffix=='.pdf':
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages)>40: raise ValueError('第一版 PDF 最多40页，请分批上传。')
            raw='\n'.join(p.extract_text() or '' for p in pdf.pages)
            records=[]
            for page_no,page in enumerate(pdf.pages,1):
                for table in page.extract_tables():
                    if not table: continue
                    clean=[[ ''.join((x or '').splitlines()).strip() for x in line] for line in table]
                    header=clean[0]
                    if '交易账户余额单位' not in header or '关联订单id' not in header: continue
                    for line in clean[1:]:
                        if line==header: continue
                        r=dict(zip(header,line));r['_page']=page_no;records.append(r)
            if records:
                control={};previous={}
                for r in sorted(records,key=lambda x:(x['时间'],x['id'])):
                    sym=r['交易账户余额单位'];balance=D(r['交易账户余额']);delta=D(r['交易账户余额变动'])
                    if sym in previous and abs(previous[sym]+delta-balance)>D('1e-8'):
                        raise ValueError('PDF 内余额不连续，可能缺页或缺行，请核对完整账单。')
                    previous[sym]=balance;control[sym]=str(balance)
                orders=defaultdict(list);rows=[]
                for r in records:
                    if r['账单类型']=='划转':
                        delta=D(r['交易账户余额变动'])
                        rows.append({'source_id':r['id'],'occurred_at':r['时间'].replace(' ','T')+'+08:00',
                          'kind':'DEPOSIT' if delta>0 else 'WITHDRAWAL','symbol':r['交易账户余额单位'],
                          'currency':r['交易账户余额单位'],'quantity':str(abs(delta)),'price':'1','fee':'0',
                          'fee_currency':r['交易账户余额单位'],'evidence':{'page':r['_page'],'raw':r}})
                    elif r['账单类型']=='币币': orders[r['关联订单id']].append(r)
                    else: raise ValueError('PDF含未支持的账单类型，请使用标准模板核对。')
                for order,legs in orders.items():
                    if len(legs)!=2: raise ValueError(f'订单{order}不止两条币种腿，需人工核对。')
                    cash=next((l for l in legs if l['交易账户余额单位']==currency),None)
                    asset=next((l for l in legs if l['交易账户余额单位']!=currency),None)
                    if not cash or not asset: raise ValueError('交易币种与账户报价币不一致。')
                    q=D(asset['数量']);p=D(asset['成交价']);f=-D(asset['手续费'])
                    kind='BUY' if D(cash['交易账户余额变动'])<0 else 'SELL'
                    if abs(q*p-abs(D(cash['交易账户余额变动'])))>D('1e-8') or D(cash['手续费'])!=0:
                        raise ValueError('成交金额/费用无法与现金腿对齐，请人工核对。')
                    expected=q-f if kind=='BUY' else -q-f
                    difference=D(asset['交易账户余额变动'])-expected
                    if abs(difference)>D('1e-8'):
                        raise ValueError('资产腿数量、费用与余额变动不一致，请人工核对。')
                    if difference:
                        notes.append(f'订单 {order}：原账单资产变动与数量减费用相差 {difference}，在末位精度范围内；按数量与明确费用入账，保留原文。')
                    rows.append({'source_id':order,'occurred_at':asset['时间'].replace(' ','T')+'+08:00','kind':kind,
                      'symbol':asset['交易账户余额单位'],'currency':currency,'quantity':str(q),'price':str(p),
                      'fee':str(f),'fee_currency':asset['手续费单位'],'evidence':{'page':asset['_page'],'raw_legs':legs}})
                notes+=['识别为 OKX 统一交易账单，两币种腿已合并为一次交易。',
                  '划转相对当前交易子账户记作流入/流出；不能据此推断全平台净投入。',
                  '该版式按北京时间解释，请在核对页确认原文件时区。']
                return {'rows':rows,'notes':notes,'raw_text':raw[:24000],
                  'closing_balances':control,'closing_at':max(r['时间'] for r in records).replace(' ','T')+'+08:00'}
            return {'rows':[],'notes':['未匹配已验证的交易表格。请对照原文手工补录，或上传标准CSV/XLSX；资金账单不推测为买卖。'],'raw_text':raw[:24000]}
    if suffix in {'.png','.jpg','.jpeg','.webp'}:
        from PIL import Image
        with Image.open(path) as im:
            if im.width*im.height>40_000_000: raise ValueError('图片超过40MP限制。')
        return {'rows':[],'notes':['图片已保存供原图核对。本版不自动下载OCR模型，请对照原图补录或使用标准表格。'],'raw_text':''}
    raise ValueError('当前支持CSV、XLSX、PDF、PNG/JPG/WebP。ZIP请先解压后上传账单。')

