"""固定八大类导航，整理旧分类并保留旧网址、图书与来源映射。"""
from alembic import op

revision = "20260831_0008"
down_revision = "20260831_0007"
branch_labels = None
depends_on = None


def upgrade():
    from scripts.migrate_catalog_v1 import migrate
    migrate(op.get_bind())


def downgrade():
    # 只回退迁移版本，不撤销业务分类整理。完整撤回须使用升级前备份，
    # 避免覆盖升级后新增图书、人工分类及来源映射。
    pass
