import time
from datetime import timedelta
from sqlalchemy import select, or_, and_
from .db import Session, Job, Batch, Account, now, uid
from .config import PRIVATE
from .adapters import parse_file
from .domain import validate

def run_once():
    with Session.begin() as s:
        job=s.scalar(select(Job).where(or_(Job.status=='queued',and_(Job.status=='running',Job.lease_until<now()))).with_for_update(skip_locked=True).limit(1))
        if not job: return False
        batch=s.get(Batch,job.batch_id)
        if job.attempts>=3:
            job.status='failed'; batch.status='failed'; batch.payload={'rows':[],'notes':['解析连续失败，请重试或转换为标准CSV。']};return True
        token=uid(); job.token=token;job.status='running';job.lease_until=now()+timedelta(minutes=5);job.attempts+=1
        batch.status='parsing'; job_id=job.id;batch_id=batch.id
        path=PRIVATE/'uploads'/batch.storage_key;currency=s.get(Account,batch.account_id).currency
    try:
        payload=parse_file(path,currency); issues=[]
        for i,r in enumerate(payload['rows']):
            try: payload['rows'][i]=validate(r,currency)
            except (ValueError,TypeError) as e: issues.append({'row':i+1,'error':str(e)})
        payload['issues']=issues;status='review'
    except Exception as e:
        payload={'rows':[],'notes':[f'解析未完成：{type(e).__name__}。请检查文件内容，或使用标准模板。'],'issues':[]};status='failed'
    with Session.begin() as s:
        job=s.scalar(select(Job).where(Job.id==job_id).with_for_update())
        if job.token==token:
            batch=s.get(Batch,batch_id);batch.payload=payload;batch.status=status;job.status='done' if status=='review' else 'failed'
    return True

if __name__=='__main__':
    import threading, os
    def price_loop():
        from .market import discover, run_quote_once
        from .db import Setting
        last_discovery=0
        while True:
            try:
                if time.monotonic()-last_discovery>60:
                    discover();last_discovery=time.monotonic()
                run_quote_once()
                with Session.begin() as s:
                    status=s.get(Setting,'quote_worker')
                    if status:status.value={'heartbeat':now().isoformat()}
                    else:s.add(Setting(key='quote_worker',value={'heartbeat':now().isoformat()}))
                time.sleep(2)
            except Exception as exc:
                print('Price worker retry: '+type(exc).__name__,flush=True);time.sleep(10)
    if os.environ.get('AUTO_QUOTES','true').lower()=='true':
        threading.Thread(target=price_loop,daemon=True,name='quote-worker').start()
    print('Import worker ready.',flush=True)
    while True:
        try:
            if not run_once(): time.sleep(2)
        except Exception as e:
            print(f'Worker retry: {type(e).__name__}',flush=True);time.sleep(5)
