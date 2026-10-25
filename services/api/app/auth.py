"""Offline username + Argon2id + RFC 6238 authentication, backed by SQLite."""
import base64, hashlib, hmac, io, ipaddress, json, os, re, secrets, time
from uuid import uuid4
import pyotp, qrcode
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from . import auth_store as store
from .db import request_owner

router=APIRouter()
COOKIE='portfolio_session'
PENDING='portfolio_enrollment'
CHALLENGE='portfolio_challenge'
PH=PasswordHasher(time_cost=3,memory_cost=65536,parallelism=4,type=Type.ID)
DUMMY_HASH=PH.hash(secrets.token_urlsafe(32))
ERROR='认证信息无效、已过期或已使用。'

def clock():return time.time()
def token_hash(value):return hashlib.sha256(value.encode()).hexdigest()
def username(value):
    value=value.strip().casefold()
    if not re.fullmatch(r'[\w.-]{2,40}',value):raise HTTPException(422,'用户名为 2–40 位字母、汉字、数字、点、短横线或下划线。')
    return value

def local_request(request):
    try:local=ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:local=False
    return local and not any(h in request.headers for h in ('forwarded','x-forwarded-for','x-real-ip'))

def cookie(response,name,value,age):
    response.set_cookie(name,value,max_age=age,httponly=True,samesite='strict',secure=os.environ.get('COOKIE_SECURE','false').lower()=='true')

def password_ok(encoded,password):
    try:return PH.verify(encoded or DUMMY_HASH,password)
    except (VerificationError,InvalidHashError):return False

def throttle(key,limit,seconds):
    key=token_hash(key);stamp=clock()
    with store.connection() as c:
        row=c.execute('SELECT * FROM throttles WHERE key=?',(key,)).fetchone()
        if not row or row['started']+seconds<=stamp:
            c.execute('INSERT OR REPLACE INTO throttles VALUES (?,?,1)',(key,stamp));return
        if row['count']>=limit:raise HTTPException(429,'尝试次数较多，请稍后再试。')
        c.execute('UPDATE throttles SET count=count+1 WHERE key=?',(key,))

def limit(request,action,name='',limit=10,seconds=300):
    throttle(action+':ip:'+request.client.host,40,300)
    if name:throttle(action+':user:'+name,limit,seconds)

def matching_step(secret,code,last=-1):
    if not re.fullmatch(r'\d{6}',code):return None
    counter=int(clock())//30;totp=pyotp.TOTP(secret,digits=6,interval=30)
    for step in (counter,counter-1,counter+1):
        if step>last and totp.verify(code,for_time=step*30,valid_window=0):return step
    return None

def consume_totp(c,user,code):
    step=matching_step(store.decrypt(user['totp_secret']),code,user['last_step'])
    if step is None:return False
    # BEGIN IMMEDIATE serializes verification and consumption across processes.
    c.execute('UPDATE users SET last_step=? WHERE id=?',(step,user['id']));return True

def invalidate(c,owner):
    for table in ('sessions','challenges','enrollments'):c.execute(f'DELETE FROM {table} WHERE user_id=?',(owner,))

def issue_session(c,response,user):
    stamp=clock();token=secrets.token_urlsafe(32)
    c.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',(token_hash(token),user['id'],user['version'],stamp+43200,stamp,stamp))
    cookie(response,COOKIE,token,43200);response.delete_cookie(CHALLENGE);response.delete_cookie(PENDING)

def identity(request):
    if not local_request(request):return None
    with store.connection() as c:
        if store.meta(c,'mode')=='local':return store.meta(c,'local_owner')
        token=request.cookies.get(COOKIE,'')
        row=c.execute('SELECT s.*,u.enabled,u.version AS current_version FROM sessions s JOIN users u ON s.user_id=u.id WHERE token_hash=?',(token_hash(token),)).fetchone() if token else None
        if not row:return None
        if not row['enabled'] or row['version']!=row['current_version'] or row['expires_at']<=clock() or row['last_seen']+1800<=clock():
            c.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash(token),));return None
        c.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?',(clock(),token_hash(token)))
        return row['user_id']

def require_user(request:Request):
    owner=request_owner.get()
    if owner in {None,'__anonymous__'}:raise HTTPException(401,'请先完成登录和二步验证。')
    return owner

def recovery_hash(owner,code):return token_hash('recovery:'+owner+':'+re.sub(r'[-\s]','',code).upper())
def recovery_exists(c,owner,code):
    return bool(code and c.execute('SELECT 1 FROM recovery_codes WHERE user_id=? AND code_hash=? AND used_at IS NULL',(owner,recovery_hash(owner,code))).fetchone())
def consume_recovery(c,owner,code):
    c.execute('UPDATE recovery_codes SET used_at=? WHERE user_id=? AND code_hash=? AND used_at IS NULL',(clock(),owner,recovery_hash(owner,code)))
def new_recovery_codes(c,owner):
    c.execute('DELETE FROM recovery_codes WHERE user_id=?',(owner,));result=[]
    for _ in range(10):
        raw=secrets.token_hex(16).upper();code='-'.join(raw[i:i+4] for i in range(0,len(raw),4))
        c.execute('INSERT INTO recovery_codes(user_id,code_hash) VALUES (?,?)',(owner,recovery_hash(owner,code)));result.append(code)
    return result

def legacy_ok(row,password):
    if not row:return False
    value=json.loads(store.decrypt(row['credential']))
    encoded=hashlib.scrypt(password.encode(),salt=bytes.fromhex(value['salt']),n=value.get('n',32768),r=8,p=value.get('p',3),maxmem=64*1024*1024).hex()
    return hmac.compare_digest(encoded,value['hash'])

def enrollment(c,response,owner,name,password,purpose,version=0):
    secret=pyotp.random_base32();token=secrets.token_urlsafe(32)
    uri=pyotp.TOTP(secret,interval=30,digits=6).provisioning_uri(name=name,issuer_name='Portfolio Observatory')
    image=io.BytesIO();qrcode.make(uri).save(image,format='PNG')
    c.execute('DELETE FROM enrollments WHERE user_id=?',(owner,))
    c.execute('INSERT INTO enrollments VALUES (?,?,?,?,?,?,?,?,0)',(token_hash(token),owner,name,PH.hash(password),store.encrypt(secret),purpose,version,clock()+600))
    cookie(response,PENDING,token,600)
    return {'qr_data_url':'data:image/png;base64,'+base64.b64encode(image.getvalue()).decode(),'manual_key':secret,'username':name,'expires_in':600,'digits':6,'period':30}

class Enrollment(BaseModel):
    username:str=Field(min_length=2,max_length=40)
    password:str=Field(min_length=12,max_length=200)
    intent:str=Field(default='register',pattern='^(register|legacy|local)$')
    legacy_username:str=Field(default='',max_length=40)
    legacy_password:str=Field(default='',max_length=200)
class Code(BaseModel):code:str=Field(pattern=r'^\d{6}$')
class PasswordLogin(BaseModel):
    username:str=Field(min_length=2,max_length=40)
    password:str=Field(min_length=1,max_length=200)
class Recover(BaseModel):
    username:str=Field(min_length=2,max_length=40)
    new_password:str=Field(min_length=12,max_length=200)
    password:str=Field(default='',max_length=200)
    code:str=Field(default='',max_length=6)
    recovery_code:str=Field(min_length=1,max_length=100)
    second_recovery_code:str=Field(default='',max_length=100)
class Proof(BaseModel):
    password:str=Field(max_length=200)
    code:str=Field(pattern=r'^\d{6}$')
class Mode(BaseModel):
    mode:str=Field(pattern='^(local|account)$')
    password:str=Field(default='',max_length=200)
    code:str=Field(default='',max_length=6)
    legacy_username:str=Field(default='',max_length=40)

@router.get('/api/auth')
def status(request:Request):
    owner=request_owner.get()
    with store.connection() as c:
        store.cleanup(c);user=c.execute('SELECT username FROM users WHERE id=?',(owner,)).fetchone()
        return {'configured':bool(c.execute('SELECT 1 FROM users LIMIT 1').fetchone()),'logged_in':owner not in {None,'__anonymous__'},
                'mode':store.meta(c,'mode'),'username':user['username'] if user else None,'totp_enabled':bool(user),
                'legacy_available':bool(c.execute('SELECT 1 FROM legacy_accounts WHERE claimed=0 LIMIT 1').fetchone()),'email_enabled':False,
                'recovery_remaining':c.execute('SELECT count(*) FROM recovery_codes WHERE user_id=? AND used_at IS NULL',(owner,)).fetchone()[0] if user else 0}

@router.post('/api/auth/enroll/start')
def start_enrollment(body:Enrollment,request:Request,response:Response):
    name=username(body.username);limit(request,'enroll',name,5,600)
    with store.connection() as c:
        store.cleanup(c)
        if store.meta(c,'mode')=='local' and body.intent!='local':raise HTTPException(409,'请在设置中为当前 Local Mode 工作空间启用认证。')
        if c.execute('SELECT 1 FROM users WHERE username=?',(name,)).fetchone():raise HTTPException(409,'用户名不可用。')
        legacy=c.execute('SELECT * FROM legacy_accounts WHERE username=? AND claimed=0',(name,)).fetchone()
        if legacy and body.intent!='legacy':raise HTTPException(409,'该用户名属于原账户，请通过原账户升级入口验证。')
        owner=str(uuid4());purpose='register'
        if body.intent=='legacy':
            lookup=body.legacy_username.strip().casefold() or 'owner'
            legacy=c.execute('SELECT * FROM legacy_accounts WHERE username=? AND claimed=0',(lookup,)).fetchone()
            if not legacy_ok(legacy,body.legacy_password):raise HTTPException(401,ERROR)
            owner=legacy['owner_id'];purpose='legacy'
            if c.execute('SELECT 1 FROM users WHERE id=?',(owner,)).fetchone():raise HTTPException(409,'原账户已经完成升级。')
        elif body.intent=='local':
            if store.meta(c,'mode')!='local':raise HTTPException(403,'请在 Local Mode 内启用账户保护。')
            owner=store.meta(c,'local_owner');purpose='local'
            if c.execute('SELECT 1 FROM users WHERE id=?',(owner,)).fetchone():raise HTTPException(409,'此本地空间已有认证账户，请切换回账户模式。')
        elif not c.execute('SELECT 1 FROM users LIMIT 1').fetchone() and not c.execute('SELECT 1 FROM legacy_accounts LIMIT 1').fetchone():owner='legacy'
        reserved=c.execute('SELECT owner_id FROM legacy_accounts WHERE username=? AND claimed=0',(name,)).fetchone()
        if reserved and reserved['owner_id']!=owner:raise HTTPException(409,'用户名属于另一个原账户。')
        return enrollment(c,response,owner,name,body.password,purpose)

@router.post('/api/auth/enroll/confirm')
def confirm_enrollment(body:Code,request:Request,response:Response):
    limit(request,'enroll-confirm')
    with store.connection() as c:
        key=token_hash(request.cookies.get(PENDING,''));e=c.execute('SELECT * FROM enrollments WHERE token_hash=?',(key,)).fetchone()
        if not e or e['expires_at']<=clock() or e['attempts']>=5:raise HTTPException(400,ERROR)
        step=matching_step(store.decrypt(e['secret']),body.code)
        if step is None:
            c.execute('UPDATE enrollments SET attempts=attempts+1 WHERE token_hash=?',(key,));c.commit();raise HTTPException(400,ERROR)
        user=c.execute('SELECT * FROM users WHERE id=?',(e['user_id'],)).fetchone()
        if e['purpose']=='recovery':
            if not user or user['version']!=e['version'] or user['enabled']:raise HTTPException(400,ERROR)
            c.execute('UPDATE users SET password_hash=?,totp_secret=?,last_step=?,enabled=1,version=version+1 WHERE id=?',(e['password_hash'],e['secret'],step,e['user_id']))
        else:
            if user or c.execute('SELECT 1 FROM users WHERE username=?',(e['username'],)).fetchone():raise HTTPException(409,'账户已存在，请重新登录。')
            c.execute('INSERT INTO users(id,username,password_hash,totp_secret,last_step,created_at) VALUES (?,?,?,?,?,?)',(e['user_id'],e['username'],e['password_hash'],e['secret'],step,clock()))
        c.execute('UPDATE legacy_accounts SET claimed=1,credential=? WHERE owner_id=?',('',e['user_id']))
        invalidate(c,e['user_id'])
        if e['purpose']=='local':c.execute("UPDATE metadata SET value='account' WHERE key='mode'")
        codes=new_recovery_codes(c,e['user_id']);user=c.execute('SELECT * FROM users WHERE id=?',(e['user_id'],)).fetchone()
        issue_session(c,response,user);store.audit(c,user['id'],'account_recovered' if e['purpose']=='recovery' else 'totp_enabled')
        return {'ok':True,'recovery_codes':codes,'message':'恢复代码仅显示这一次，请离线保存。'}

@router.post('/api/auth/login')
def login(body:PasswordLogin,request:Request,response:Response):
    name=username(body.username);limit(request,'login',name,5,300)
    with store.connection() as c:
        user=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
        valid=password_ok(user['password_hash'] if user else DUMMY_HASH,body.password)
        if not valid or not user or not user['enabled']:raise HTTPException(401,ERROR)
        if PH.check_needs_rehash(user['password_hash']):c.execute('UPDATE users SET password_hash=? WHERE id=?',(PH.hash(body.password),user['id']))
        c.execute('DELETE FROM challenges WHERE token_hash=?',(token_hash(request.cookies.get(CHALLENGE,'')),))
        token=secrets.token_urlsafe(32);c.execute('INSERT INTO challenges VALUES (?,?,?,?,0)',(token_hash(token),user['id'],user['version'],clock()+300))
        cookie(response,CHALLENGE,token,300)
    return {'requires_totp':True,'expires_in':300}

@router.post('/api/auth/login/verify')
def verify_login(body:Code,request:Request,response:Response):
    limit(request,'totp')
    with store.connection() as c:
        key=token_hash(request.cookies.get(CHALLENGE,''));challenge=c.execute('SELECT * FROM challenges WHERE token_hash=?',(key,)).fetchone()
        if not challenge or challenge['expires_at']<=clock() or challenge['attempts']>=5:raise HTTPException(401,ERROR)
        user=c.execute('SELECT * FROM users WHERE id=?',(challenge['user_id'],)).fetchone()
        if not user or not user['enabled'] or user['version']!=challenge['version']:raise HTTPException(401,ERROR)
        if not consume_totp(c,user,body.code):
            c.execute('UPDATE challenges SET attempts=attempts+1 WHERE token_hash=?',(key,));c.commit();raise HTTPException(401,ERROR)
        c.execute('DELETE FROM challenges WHERE token_hash=?',(key,))
        c.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash(request.cookies.get(COOKIE,'')),))
        issue_session(c,response,user);store.audit(c,user['id'],'login')
    return {'ok':True}

@router.post('/api/auth/recover')
def recover(body:Recover,request:Request,response:Response):
    name=username(body.username);limit(request,'recover',name,5,900)
    with store.connection() as c:
        user=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
        valid_password=password_ok(user['password_hash'] if user else DUMMY_HASH,body.password)
        if not user or not recovery_exists(c,user['id'],body.recovery_code):raise HTTPException(401,ERROR)
        second=(recovery_hash(user['id'],body.recovery_code)!=recovery_hash(user['id'],body.second_recovery_code) and recovery_exists(c,user['id'],body.second_recovery_code))
        if not valid_password and not second and not consume_totp(c,user,body.code):raise HTTPException(401,ERROR)
        result=enrollment(c,response,user['id'],user['username'],body.new_password,'recovery',user['version']+1)
        consume_recovery(c,user['id'],body.recovery_code)
        if not valid_password and second:consume_recovery(c,user['id'],body.second_recovery_code)
        c.execute('UPDATE users SET enabled=0,version=version+1 WHERE id=?',(user['id'],))
        if store.meta(c,'mode')=='local' and store.meta(c,'local_owner')==user['id']:
            c.execute("UPDATE metadata SET value='account' WHERE key='mode'")
        c.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],));c.execute('DELETE FROM challenges WHERE user_id=?',(user['id'],))
        response.delete_cookie(COOKIE);response.delete_cookie(CHALLENGE);store.audit(c,user['id'],'recovery_started')
        return result

@router.post('/api/auth/recovery-codes/rotate')
def rotate_codes(body:Proof,request:Request):
    owner=require_user(request);limit(request,'rotate',owner,5,900)
    with store.connection() as c:
        if store.meta(c,'mode')!='account':raise HTTPException(403,'请先切换到账户模式。')
        user=c.execute('SELECT * FROM users WHERE id=?',(owner,)).fetchone()
        if not user or not password_ok(user['password_hash'],body.password) or not consume_totp(c,user,body.code):raise HTTPException(401,ERROR)
        codes=new_recovery_codes(c,owner);store.audit(c,owner,'recovery_codes_rotated');return {'recovery_codes':codes}

@router.post('/api/auth/mode')
def set_mode(body:Mode,request:Request,response:Response):
    limit(request,'mode',request_owner.get() or 'initial',5,900)
    with store.connection() as c:
        current=store.meta(c,'mode')
        if current==body.mode:return {'ok':True}
        owner=request_owner.get();user=c.execute('SELECT * FROM users WHERE id=?',(owner,)).fetchone()
        any_user=c.execute('SELECT 1 FROM users LIMIT 1').fetchone()
        if user:
            if not user['enabled'] or not password_ok(user['password_hash'],body.password) or not consume_totp(c,user,body.code):raise HTTPException(401,ERROR)
        elif any_user:raise HTTPException(403,'切换模式需要当前账户的密码和 Authenticator 验证。')
        elif current=='local':raise HTTPException(409,'请先在 Local Mode 中注册并绑定 Authenticator。')
        else:
            records=c.execute('SELECT * FROM legacy_accounts WHERE claimed=0').fetchall()
            legacy=next((r for r in records if r['username']==(body.legacy_username.strip().casefold() or 'owner')),None)
            if records and not legacy_ok(legacy,body.password):raise HTTPException(401,ERROR)
            owner=legacy['owner_id'] if legacy else 'legacy'
        c.execute('UPDATE metadata SET value=? WHERE key=?',(body.mode,'mode'))
        c.execute("UPDATE metadata SET value=? WHERE key='local_owner'",(owner,))
        for table in ('sessions','challenges','enrollments'):c.execute('DELETE FROM '+table)
        store.audit(c,owner,'mode_'+body.mode)
        for name in (COOKIE,PENDING,CHALLENGE):response.delete_cookie(name)
    return {'ok':True}

@router.post('/api/logout')
def logout(request:Request,response:Response):
    with store.connection() as c:
        for table,name in (('sessions',COOKIE),('challenges',CHALLENGE),('enrollments',PENDING)):
            c.execute('DELETE FROM '+table+' WHERE token_hash=?',(token_hash(request.cookies.get(name,'')),));response.delete_cookie(name)
    return {'ok':True}
