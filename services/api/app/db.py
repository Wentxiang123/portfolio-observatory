from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import create_engine, String, Text, DateTime, Numeric, JSON, Integer, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker, with_loader_criteria
from sqlalchemy import event
from contextvars import ContextVar
from .config import DATABASE_URL

def now():
    return datetime.now(timezone.utc)

def uid():
    return str(uuid4())

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)
request_owner = ContextVar('request_owner', default=None)

def ledger_key():
    owner=request_owner.get()
    return 'ledger_version' if owner in {None,'legacy'} else 'ledger:'+owner

class Owned:
    owner_id: Mapped[str] = mapped_column(String(36), default='legacy', index=True)

class Base(DeclarativeBase):
    pass

class Setting(Base):
    __tablename__ = 'settings'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)

class Account(Owned, Base):
    __tablename__ = 'accounts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    platform: Mapped[str] = mapped_column(String(30))
    currency: Mapped[str] = mapped_column(String(20))
    confirmed_at: Mapped[str | None] = mapped_column(String(60), nullable=True)

class Batch(Owned, Base):
    __tablename__ = 'import_batches'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30), default='queued')
    version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Event(Owned, Base):
    __tablename__ = 'events'
    __table_args__ = (UniqueConstraint('account_id', 'source_id'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    source_id: Mapped[str] = mapped_column(String(200))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[object] = mapped_column(Numeric(60,24))
    price: Mapped[object] = mapped_column(Numeric(60,24))
    fee: Mapped[object] = mapped_column(Numeric(60,24))
    fee_currency: Mapped[str] = mapped_column(String(40))
    active: Mapped[int] = mapped_column(Integer, default=1)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)

class Quote(Owned, Base):
    __tablename__ = 'quotes'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    currency: Mapped[str] = mapped_column(String(20))
    price: Mapped[object] = mapped_column(Numeric(60,24))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(150))
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    instrument_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

class Job(Base):
    __tablename__ = 'jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    token: Mapped[str | None] = mapped_column(String(36), nullable=True)

class Audit(Owned, Base):
    __tablename__ = 'audit_log'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    action: Mapped[str] = mapped_column(String(40))
    target: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class SavedReport(Owned, Base):
    __tablename__ = 'reports'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(String(36))
    ledger_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    username: Mapped[str] = mapped_column(String(40), unique=True)
    salt: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(Text)
    session_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class EmailChallenge(Base):
    __tablename__ = 'email_challenges'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[int] = mapped_column(Integer, default=0)

class AuthThrottle(Base):
    __tablename__ = 'auth_throttles'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    count: Mapped[int] = mapped_column(Integer, default=0)

class Instrument(Owned, Base):
    __tablename__ = 'instruments'
    __table_args__ = (UniqueConstraint('account_id', 'symbol'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(20))
    asset_type: Mapped[str] = mapped_column(String(20))
    market: Mapped[str] = mapped_column(String(20))
    provider_symbol: Mapped[str] = mapped_column(String(60))
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    mapping_revision: Mapped[int] = mapped_column(Integer, default=0)
    next_poll: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(250), nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)

@event.listens_for(Session, 'do_orm_execute')
def isolate_reads(state):
    """HTTP identity is propagated to sync handlers by ContextVar; workers run unscoped."""
    owner = request_owner.get()
    if owner is not None:
        state.statement = state.statement.options(with_loader_criteria(
            Owned, lambda cls: cls.owner_id == owner, include_aliases=True))

@event.listens_for(Session, 'before_flush')
def isolate_writes(session, context, instances):
    owner = request_owner.get()
    if owner is None:
        return  # Trusted local bootstrap, migration and workers only.
    for obj in session.new:
        if isinstance(obj, Owned):
            if obj.owner_id and obj.owner_id != owner:
                raise ValueError('不能写入其他用户的数据。')
            obj.owner_id = owner
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, Owned) and obj.owner_id != owner:
            raise ValueError('不能修改其他用户的数据。')

