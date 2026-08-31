"""固定图书 ID 网址：本地 SQLite 也禁止复用已删除图书的编号。"""
from alembic import op

revision = "20260831_0006"
down_revision = "20260831_0005"
branch_labels = None
depends_on = None


def upgrade():
    # MySQL 主键已有 AUTO_INCREMENT；无须改字段或重排编号。
    if op.get_bind().dialect.name == "sqlite":
        definition = op.get_bind().exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='resources'").scalar_one()
        if "AUTOINCREMENT" in definition.upper():
            return
        with op.batch_alter_table("resources", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
            pass


def downgrade():
    # 保留更强的 ID 不复用保证，对旧版代码兼容；不回收或重排已使用编号。
    pass
