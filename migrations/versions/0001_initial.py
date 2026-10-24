"""Initial personal portfolio schema."""
from alembic import op
from app.db import Base
revision='0001'
down_revision=None
branch_labels=None
depends_on=None
def upgrade():
    Base.metadata.create_all(bind=op.get_bind())
def downgrade():
    raise RuntimeError('本版本不提供破坏性自动降级；请恢复备份。')
