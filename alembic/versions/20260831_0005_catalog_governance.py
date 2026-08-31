"""分类映射、旧分类跳转与人工审核保护；不自动合并或下架旧数据。"""
from alembic import op
import sqlalchemy as sa

revision = "20260831_0005"
down_revision = "20260831_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("resources", sa.Column("metadata_locked", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("resources", sa.Column("source_category_main", sa.String(100)))
    op.add_column("resources", sa.Column("source_category_sub", sa.String(100)))
    op.create_table(
        "category_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_main", sa.String(100), nullable=False),
        sa.Column("source_sub", sa.String(100), nullable=False),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_main", "source_sub", name="uq_category_mapping_source"),
    )
    op.create_table(
        "category_redirects",
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("target_id", sa.Integer(), sa.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False),
    )
    # 后台单书编辑有明确审计证据：升级后也保护这些人工编辑记录。
    # 不凭“已发布”推断人工审核，不改原版权/发布状态、编号、slug。
    op.execute(sa.text("""UPDATE resources SET metadata_locked = true WHERE
        EXISTS (SELECT 1 FROM admin_operation_logs l WHERE l.entity_type = 'resource'
        AND l.action = 'update' AND l.entity_id = CAST(resources.id AS CHAR))"""))


def downgrade():
    op.drop_table("category_redirects")
    op.drop_table("category_mappings")
    with op.batch_alter_table("resources") as batch:
        batch.drop_column("source_category_sub")
        batch.drop_column("source_category_main")
        batch.drop_column("metadata_locked")
