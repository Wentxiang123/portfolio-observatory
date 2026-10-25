"""Archived v0.3 email adapter. NOT mounted by the application.

SMTP configuration and implementation are retained for future extraction. Do not mount
this legacy router: its password/email-only sessions do not enforce the current TOTP policy.
"""
import hashlib
import hmac
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from .config import PRIVATE
from .db import Session, Setting, User, EmailChallenge, AuthThrottle, now, request_owner

router = APIRouter()
COOKIE = 'portfolio_session'

def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=3, maxmem=64*1024*1024).hex()

def secret():
    configured = os.environ.get('APP_SECRET')
    if configured:
        if len(configured) < 32: raise RuntimeError('APP_SECRET must have at least 32 characters')
        return configured.encode()
    path = PRIVATE / 'auth-secret'
    try:
        with path.open('xb') as f: f.write(secrets.token_bytes(32))
    except FileExistsError: pass
    return path.read_bytes()

def digest(value): return hmac.new(secret(), value.encode(), hashlib.sha256).hexdigest()

def email_address(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r'[a-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+', value):
        raise ValueError('请输入有效邮箱。')
    return value

def smtp_ready():
    return bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'))

def send_code(email, code, purpose):
    if not smtp_ready(): raise HTTPException(503, '邮件服务尚未配置，请按配置说明设置 SMTP 后使用邮箱验证。')
    mode = os.environ.get('SMTP_SECURITY', 'starttls').lower()
    if mode not in {'starttls', 'ssl'}: raise HTTPException(503, 'SMTP_SECURITY 必须为 starttls 或 ssl。')
    message = EmailMessage()
    message['From'] = os.environ['SMTP_FROM']; message['To'] = email
    message['Subject'] = '持仓观测站 · 邮箱验证码'
    label = '重置密码' if purpose == 'reset' else '验证邮箱'
    message.set_content(f'你正在{label}，验证码：{code}\n10 分钟内有效，请勿分享。若非本人操作，请忽略此邮件。')
    cls = smtplib.SMTP_SSL if mode == 'ssl' else smtplib.SMTP
    options = {'context': ssl.create_default_context()} if mode == 'ssl' else {}
    try:
        with cls(os.environ['SMTP_HOST'], int(os.environ.get('SMTP_PORT', '465' if mode == 'ssl' else '587')), timeout=10, **options) as smtp:
            if mode == 'starttls': smtp.starttls(context=ssl.create_default_context())
            if os.environ.get('SMTP_USERNAME'): smtp.login(os.environ['SMTP_USERNAME'], os.environ.get('SMTP_PASSWORD', ''))
            smtp.send_message(message)
    except Exception:
        raise HTTPException(503, '邮件暂时发送失败，请检查本机 SMTP 配置或稍后重试。') from None

def throttle(key, limit, seconds):
    key = digest(key)
    with Session.begin() as s:
        # Serialize creation too: a missing row cannot be protected with FOR UPDATE.
        s.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(key[:15], 16)})
        row = s.get(AuthThrottle, key)
        if row is None: row = AuthThrottle(key=key, started_at=now(), count=0); s.add(row)
        if row.started_at + timedelta(seconds=seconds) <= now(): row.started_at=now(); row.count=0
        if row.count >= limit: raise HTTPException(429, '尝试次数较多，请稍后再试。')
        row.count += 1

def identity(request):
    token = request.cookies.get(COOKIE, '')
    if not token: return None
    with Session() as s:
        session = s.get(Setting, 'session:'+hashlib.sha256(token.encode()).hexdigest())
        if not session or datetime.fromisoformat(session.value['expires']) <= now(): return None
        owner = session.value.get('user_id', 'legacy')
        user = s.get(User, owner)
        if user and user.session_version != session.value.get('version', 0): return None
        if not user and owner != 'legacy': return None
        return owner

def require_user(request: Request):
    owner = request_owner.get()
    if not owner or owner == '__anonymous__': raise HTTPException(401, '请先登录。')
    return owner

def issue_session(s, response, owner, version=0):
    token = secrets.token_urlsafe(32)
    s.add(Setting(key='session:'+hashlib.sha256(token.encode()).hexdigest(), value={
        'user_id':owner,'version':version,'expires':(now()+timedelta(hours=12)).isoformat()}))
    response.set_cookie(COOKIE, token, httponly=True, samesite='strict', max_age=43200,
                        secure=os.environ.get('COOKIE_SECURE', 'false').lower() == 'true')

class Login(BaseModel):
    password: str = Field(min_length=8, max_length=200)
    email: str = Field(default='', max_length=254)

class CodeRequest(BaseModel):
    email: str = Field(max_length=254)
    purpose: str = Field(pattern='^(register|reset|bind)$')

class Registration(BaseModel):
    email: str = Field(max_length=254)
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=12, max_length=200)
    code: str = Field(pattern=r'^\d{6}$')

class Reset(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=12, max_length=200)
    code: str = Field(pattern=r'^\d{6}$')

@router.get('/api/auth')
def status(request: Request):
    with Session() as s:
        legacy = s.get(Setting, 'auth')
        user = s.get(User, request_owner.get())
        return {'configured':bool(legacy and legacy.value), 'logged_in':request_owner.get() not in {None, '__anonymous__'},
                'email_configured':smtp_ready(), 'legacy_available':not bool(s.get(User, 'legacy')),
                'needs_email_binding':request_owner.get() == 'legacy' and not user,
                'username':user.username if user else None, 'email':user.email if user else None}

@router.post('/api/auth/code')
def request_code(body: CodeRequest, request: Request):
    email = email_address(body.email)
    if body.purpose == 'bind' and require_user(request) != 'legacy': raise HTTPException(403, '仅原本地账户可绑定原有持仓。')
    if not smtp_ready(): raise HTTPException(503, '邮件服务尚未配置，请先填写本地 SMTP 配置。')
    throttle('mail-ip:'+request.client.host, 10, 3600)
    throttle('mail-email:'+email, 1, 60)
    throttle('mail-day:'+email, 20, 86400)
    key = body.purpose+':'+digest(email)
    code = f'{secrets.randbelow(1000000):06d}'
    with Session.begin() as s:
        user = s.scalar(select(User).where(User.email == email))
        allowed = bool(user) if body.purpose == 'reset' else not bool(user)
        row = s.get(EmailChallenge, key)
        values = dict(code_hash=digest(key+':'+code),expires_at=now()+timedelta(minutes=10), sent_at=now(), attempts=0, consumed=0)
        if row:
            for k,v in values.items(): setattr(row,k,v)
        else: row=EmailChallenge(key=key,**values); s.add(row)
        if allowed: send_code(email, code, body.purpose)
        else: row.consumed=1
    return {'ok':True,'message':'如果该邮箱符合操作条件，验证码将发送到邮箱，10 分钟内有效。'}

def verify_code(s, email, purpose, code):
    key = purpose+':'+digest(email)
    row = s.scalar(select(EmailChallenge).where(EmailChallenge.key == key).with_for_update())
    if not row or row.consumed or row.expires_at <= now() or row.attempts >= 5:
        raise HTTPException(400, '验证码无效或已过期，请重新申请。')
    row.attempts += 1
    if not hmac.compare_digest(row.code_hash, digest(key+':'+code)):
        s.commit()  # Failed attempts must survive the HTTP error.
        raise HTTPException(400, '验证码无效或已过期，请重新申请。')
    row.consumed=1

@router.post('/api/auth/register')
@router.post('/api/auth/bind')
def register(body: Registration, request: Request, response: Response):
    bind = request.url.path.endswith('/bind')
    if bind and require_user(request) != 'legacy': raise HTTPException(403, '请先用原本地密码登录。')
    email = email_address(body.email); username=body.username.strip().lower()
    if not re.fullmatch(r'[\w.-]{2,40}', username): raise ValueError('用户名限 2–40 位字母、汉字、数字、下划线、点或短横线。')
    throttle('register:'+request.client.host, 20, 3600)
    try:
        with Session.begin() as s:
            verify_code(s, email, 'bind' if bind else 'register', body.code)
            if bind and s.get(User, 'legacy'): raise HTTPException(409, '原账户已经绑定邮箱，请用邮箱登录。')
            salt=secrets.token_hex(16)
            user=User(email=email,username=username,salt=salt,password_hash=password_hash(body.password,salt))
            if bind: user.id='legacy'; user.session_version=1
            s.add(user); s.flush()
            if not bind: s.add(Setting(key='ledger:'+user.id,value={'n':0}))
            if bind: s.get(Setting,'auth').value={'bound':True}
            issue_session(s,response,user.id,user.session_version)
    except IntegrityError: raise HTTPException(409, '邮箱或用户名不可用，请更换后重试。') from None
    return {'ok':True}

@router.post('/api/auth/reset')
def reset_password(body: Reset, request: Request):
    email=email_address(body.email)
    throttle('reset:'+request.client.host, 20, 3600)
    with Session.begin() as s:
        verify_code(s,email,'reset',body.code)
        user=s.scalar(select(User).where(User.email==email).with_for_update())
        if not user: raise HTTPException(400, '验证码无效或已过期，请重新申请。')
        user.salt=secrets.token_hex(16);user.password_hash=password_hash(body.password,user.salt);user.session_version+=1
    return {'ok':True,'message':'密码已重置，所有旧会话已失效，请重新登录。'}

@router.post('/api/auth/{action}')
def login(action: str, body: Login, response: Response, request: Request):
    throttle('login-ip:'+request.client.host, 30, 60)
    if body.email:
        if action != 'login': raise HTTPException(404)
        email=email_address(body.email); throttle('login-email:'+email, 5, 60)
        with Session.begin() as s:
            user=s.scalar(select(User).where(User.email==email))
            hashed=password_hash(body.password, user.salt if user else '00'*16)
            if not user or not hmac.compare_digest(hashed,user.password_hash): raise HTTPException(401,'邮箱或密码不正确。')
            issue_session(s,response,user.id,user.session_version)
        return {'ok':True}
    # Transitional local login preserves existing holdings, until email binding succeeds.
    with Session.begin() as s:
        setting=s.scalar(select(Setting).where(Setting.key=='auth').with_for_update())
        if s.get(User,'legacy'): raise HTTPException(409,'账户已绑定邮箱，请使用邮箱登录。')
        if action=='setup':
            if setting.value: raise HTTPException(409,'已设置密码，请登录。')
            salt=secrets.token_hex(16)
            setting.value={'salt':salt,'hash':password_hash(body.password,salt),'n':32768,'p':3,'failures':0}
        elif action=='login':
            value=setting.value
            if not value: raise HTTPException(409,'请先设置本地访问密码。')
            if value.get('locked_until') and datetime.fromisoformat(value['locked_until'])>now(): raise HTTPException(429,'尝试次数较多，请稍后再试。')
            hashed=hashlib.scrypt(body.password.encode(),salt=bytes.fromhex(value['salt']),n=value.get('n',16384),r=8,p=value.get('p',1),maxmem=64*1024*1024).hex()
            if not hmac.compare_digest(hashed,value['hash']):
                failures=value.get('failures',0)+1
                setting.value={**value,'failures':failures,'locked_until':(now()+timedelta(seconds=60)).isoformat() if failures>=5 else None}
                s.commit();raise HTTPException(401,'密码不正确。')
            setting.value={**value,'failures':0,'locked_until':None}
        else: raise HTTPException(404)
        issue_session(s,response,'legacy')
    return {'ok':True}

@router.post('/api/logout')
def logout(request: Request, response: Response):
    with Session.begin() as s:
        item=s.get(Setting,'session:'+hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest())
        if item:s.delete(item)
    response.delete_cookie(COOKIE);return {'ok':True}
