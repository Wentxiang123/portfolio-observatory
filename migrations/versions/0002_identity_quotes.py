"""Email identities, private portfolios and instrument quote scheduling."""
from alembic import op
import sqlalchemy as sa
from app.db import Base
revision = '0002'
down_revision = '0001'
branch_labels = depends_on = None

def upgrade():
    bind = op.get_bind()
    # 0001 historically creates current metadata, so fresh installs already have these columns.
    for table in ('accounts','import_batches','events','quotes','audit_log','reports'):
        columns = {c['name'] for c in sa.inspect(bind).get_columns(table)}
        if 'owner_id' not in columns:
            op.add_column(table, sa.Column('owner_id',sa.String(36),nullable=False,server_default='legacy'))
            op.create_index('ix_'+table+'_owner_id',table,['owner_id'])
    if 'instrument_id' not in {c['name'] for c in sa.inspect(bind).get_columns('quotes')}:
        op.add_column('quotes',sa.Column('instrument_id',sa.String(36),nullable=True))
        op.create_index('ix_quotes_instrument_id','quotes',['instrument_id'])
    Base.metadata.create_all(bind=bind)

def downgrade():
    raise RuntimeError('请使用升级前备份恢复；不自动删除身份或账本数据。')
