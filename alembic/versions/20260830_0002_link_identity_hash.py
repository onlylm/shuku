"""分享链接判重忽略提取码。

Revision ID: 20260830_0002
Revises: 20260830_0001
Create Date: 2026-08-30
"""
from __future__ import annotations

import hashlib
from typing import Sequence, Union
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import op


revision: str = "20260830_0002"
down_revision: Union[str, Sequence[str], None] = "20260830_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _identity_hash(url: str) -> str:
    parts = urlsplit(url)
    identity = urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), "", ""))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, normalized_url FROM channel_share_links ORDER BY id")
    ).mappings()
    used: set[str] = set()
    for row in rows:
        digest = _identity_hash(row["normalized_url"])
        values: dict[str, object] = {"id": row["id"], "digest": digest}
        statement = "UPDATE channel_share_links SET normalized_url_hash = :digest WHERE id = :id"
        if digest in used:
            values["digest"] = hashlib.sha256(f"duplicate:{row['id']}:{digest}".encode()).hexdigest()
            values["error"] = "迁移发现同一分享的重复记录，已隐藏，请人工处理"
            statement = (
                "UPDATE channel_share_links SET normalized_url_hash = :digest, status = 'disabled', "
                "is_visible = 0, last_error = :error WHERE id = :id"
            )
        connection.execute(sa.text(statement), values)
        used.add(digest)


def downgrade() -> None:
    # 旧哈希包含提取码，无法从当前哈希无损恢复；URL 数据本身没有被修改。
    pass
