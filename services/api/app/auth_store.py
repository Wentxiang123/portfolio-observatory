"""Local authentication persistence; no SMTP, network or portfolio database dependency."""
import ctypes
from ctypes import wintypes
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import time
from cryptography.fernet import Fernet
from .config import PRIVATE

AUTH_DIR = PRIVATE / 'authentication'

def dpapi(data: bytes, decrypt=False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    target=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    fn=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype=wintypes.BOOL
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN; deliberately NOT CRYPTPROTECT_LOCAL_MACHINE.
    if not fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(target)):
        raise RuntimeError('无法访问当前操作系统用户的认证密钥。请使用创建该工作台的系统账户运行。')
    try:return ctypes.string_at(target.data,target.size)
    finally:kernel.LocalFree(target.data)

def cipher():
    AUTH_DIR.mkdir(parents=True,exist_ok=True)
    if os.name=='nt':
        path=AUTH_DIR/'masterkey.dpapi'
        if not path.exists():
            # Lost keys must not be silently replaced if encrypted state already exists.
            dbpath=AUTH_DIR/'auth.sqlite3'
            if dbpath.exists():
                with sqlite3.connect(dbpath) as c:
                    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table in ('users','enrollments','legacy_accounts'):
                        if table in tables and c.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
                            raise RuntimeError('认证密钥文件缺失，请恢复原备份；不会自动覆盖加密密钥。')
            protected=dpapi(Fernet.generate_key())
            try:
                with path.open('xb') as f:f.write(protected)
            except FileExistsError:pass
        return Fernet(dpapi(path.read_bytes(),decrypt=True))
    # Non-Windows: accept OS credential stores only, never a plaintext fallback backend.
    import keyring
    backend=keyring.get_keyring()
    if not type(backend).__module__.startswith(('keyring.backends.macOS','keyring.backends.SecretService')):
        raise RuntimeError('需要 macOS Keychain 或 Linux Secret Service；不使用明文密钥回退。')
    account=hashlib.sha256(str(AUTH_DIR.resolve()).encode()).hexdigest()
    key=keyring.get_password('PortfolioObservatory.TOTP',account)
    if not key:
        if (AUTH_DIR/'auth.sqlite3').exists():raise RuntimeError('系统密钥缺失，请恢复原系统密钥。')
        key=Fernet.generate_key().decode();keyring.set_password('PortfolioObservatory.TOTP',account,key)
    return Fernet(key.encode())

def encrypt(value):return cipher().encrypt(value.encode()).decode()
def decrypt(value):return cipher().decrypt(value.encode()).decode()

SCHEMA='''
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 totp_secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, last_step INTEGER NOT NULL DEFAULT -1,
 version INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS recovery_codes (
 user_id TEXT NOT NULL, code_hash TEXT NOT NULL, used_at REAL,
 PRIMARY KEY(user_id,code_hash), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL,
 expires_at REAL NOT NULL, last_seen REAL NOT NULL, created_at REAL NOT NULL,
 FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS challenges (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, version INTEGER NOT NULL,
 expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS enrollments (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, username TEXT NOT NULL,
 password_hash TEXT NOT NULL, secret TEXT NOT NULL, purpose TEXT NOT NULL,
 version INTEGER NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS throttles (key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_accounts (
 owner_id TEXT PRIMARY KEY, username TEXT UNIQUE, credential TEXT NOT NULL, claimed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, user_id TEXT, action TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS enrollments_expiry ON enrollments(expires_at);
'''

def initialize():
    AUTH_DIR.mkdir(parents=True,exist_ok=True)
    # Initialize protected key before SQLite exists (also permits OS keyring provisioning).
    cipher()
    with sqlite3.connect(AUTH_DIR/'auth.sqlite3',timeout=15) as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO metadata VALUES ('mode','account')")
        c.execute("INSERT OR IGNORE INTO metadata VALUES ('local_owner','legacy')")
        c.execute('PRAGMA user_version=1')

@contextmanager
def connection():
    if not (AUTH_DIR/'auth.sqlite3').exists():initialize()
    c=sqlite3.connect(AUTH_DIR/'auth.sqlite3',timeout=15,isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA secure_delete=ON')
    c.execute('BEGIN IMMEDIATE')
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback();raise
    finally:c.close()

def meta(c,key):
    row=c.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
    return row['value'] if row else None

def audit(c,owner,action):
    c.execute('INSERT INTO audit(user_id,action,created_at) VALUES (?,?,?)',(owner,action,time.time()))

def cleanup(c):
    stamp=time.time()
    for table in ('sessions','challenges','enrollments'):
        c.execute(f'DELETE FROM {table} WHERE expires_at<?',(stamp,))
    c.execute('DELETE FROM throttles WHERE started<?',(stamp-86400,))
