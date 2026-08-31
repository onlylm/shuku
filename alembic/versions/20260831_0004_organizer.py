"""整理软件同步授权、外部身份、预检与回执。"""
from alembic import op
import sqlalchemy as sa

revision = "20260831_0004"
down_revision = "51108abe030f"
branch_labels = None
depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False)]


def upgrade():
    op.create_table("organizer_tokens", sa.Column("id", sa.Integer, primary_key=True), sa.Column("admin_user_id", sa.Integer, sa.ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False), sa.Column("label", sa.String(100), nullable=False), sa.Column("token_hash", sa.String(64), unique=True, nullable=False), sa.Column("is_active", sa.Boolean, nullable=False), *timestamps())
    op.create_table("organizer_identities", sa.Column("book_id", sa.String(35), primary_key=True), sa.Column("resource_id", sa.Integer, sa.ForeignKey("resources.id", ondelete="CASCADE"), unique=True, nullable=False), sa.Column("epub_sha256", sa.String(64), nullable=False), sa.Column("revision", sa.Integer, nullable=False), sa.Column("payload_hash", sa.String(64), nullable=False), *timestamps())
    op.create_table("organizer_batches", sa.Column("id", sa.String(32), primary_key=True), sa.Column("token_id", sa.Integer, sa.ForeignKey("organizer_tokens.id", ondelete="CASCADE"), nullable=False), sa.Column("payload_hash", sa.String(64), nullable=False), sa.Column("payload", sa.JSON, nullable=False), sa.Column("preview", sa.JSON, nullable=False), sa.Column("receipt", sa.JSON, nullable=False), *timestamps())


def downgrade():
    op.drop_table("organizer_batches")
    op.drop_table("organizer_identities")
    op.drop_table("organizer_tokens")
