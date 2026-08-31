from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, utcnow


resource_categories = Table(
    "resource_categories",
    Base.metadata,
    Column("resource_id", ForeignKey("resources.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
)


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100), default="管理员")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Category(TimestampMixin, Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    parent: Mapped["Category | None"] = relationship(remote_side="Category.id", back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent")
    resources: Mapped[list["Resource"]] = relationship(
        secondary=resource_categories, back_populates="categories"
    )


class Resource(TimestampMixin, Base):
    __tablename__ = "resources"
    # ID 用作永久公开地址；SQLite 删除最大编号后也不得把该编号分给新书。
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    resource_type: Mapped[str] = mapped_column(String(30), default="book", index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    normalized_title: Mapped[str] = mapped_column(String(255), index=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    subtitle: Mapped[str | None] = mapped_column(String(255))
    author: Mapped[str | None] = mapped_column(String(255), index=True)
    translator: Mapped[str | None] = mapped_column(String(255))
    publisher: Mapped[str | None] = mapped_column(String(255))
    isbn: Mapped[str | None] = mapped_column(String(32), index=True)
    language: Mapped[str] = mapped_column(String(30), default="zh-CN")
    publish_year: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text)
    cover_image: Mapped[str | None] = mapped_column(String(500))
    formats: Mapped[str | None] = mapped_column(String(120))
    copyright_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    source_reference: Mapped[str | None] = mapped_column(String(500))
    publish_status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    seo_title: Mapped[str | None] = mapped_column(String(255))
    seo_description: Mapped[str | None] = mapped_column(String(500))
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    metadata_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    source_category_main: Mapped[str | None] = mapped_column(String(100))
    source_category_sub: Mapped[str | None] = mapped_column(String(100))

    categories: Mapped[list[Category]] = relationship(
        secondary=resource_categories, back_populates="resources"
    )
    files: Mapped[list["ResourceFile"]] = relationship(
        back_populates="resource", cascade="all, delete-orphan"
    )
    channels: Mapped[list["ResourceChannel"]] = relationship(
        back_populates="resource", cascade="all, delete-orphan"
    )


class ResourceFile(TimestampMixin, Base):
    __tablename__ = "resource_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    file_name: Mapped[str] = mapped_column(String(255))
    file_format: Mapped[str | None] = mapped_column(String(30))
    file_size: Mapped[int | None] = mapped_column(Integer)
    local_path: Mapped[str | None] = mapped_column(String(500))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(30), default="manual")

    resource: Mapped[Resource] = relationship(back_populates="files")


class Provider(TimestampMixin, Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    base_domain: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    accounts: Mapped[list["ProviderAccount"]] = relationship(back_populates="provider")
    channels: Mapped[list["ResourceChannel"]] = relationship(back_populates="provider")


class ProviderAccount(TimestampMixin, Base):
    __tablename__ = "provider_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    credential_reference: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="active")

    provider: Mapped[Provider] = relationship(back_populates="accounts")
    channels: Mapped[list["ResourceChannel"]] = relationship(back_populates="provider_account")


class ResourceChannel(TimestampMixin, Base):
    __tablename__ = "resource_channels"
    __table_args__ = (
        UniqueConstraint("resource_id", "provider_id", name="uq_channel_resource_provider"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"), index=True)
    provider_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="SET NULL"), index=True
    )
    provider_file_id: Mapped[str | None] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    note: Mapped[str | None] = mapped_column(String(500))

    resource: Mapped[Resource] = relationship(back_populates="channels")
    provider: Mapped[Provider] = relationship(back_populates="channels")
    provider_account: Mapped[ProviderAccount | None] = relationship(back_populates="channels")
    share_links: Mapped[list["ChannelShareLink"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ChannelShareLink(TimestampMixin, Base):
    __tablename__ = "channel_share_links"
    __table_args__ = (
        UniqueConstraint("normalized_url_hash", name="uq_share_link_normalized_hash"),
        Index("ix_share_link_front_status", "is_visible", "status", "priority"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("resource_channels.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"), index=True)
    provider_share_id: Mapped[str] = mapped_column(String(255), index=True)
    share_url: Mapped[str] = mapped_column(String(1000))
    normalized_url: Mapped[str] = mapped_column(String(1000))
    normalized_url_hash: Mapped[str] = mapped_column(String(64))
    extract_code: Mapped[str | None] = mapped_column(String(20))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))

    channel: Mapped[ResourceChannel] = relationship(back_populates="share_links")
    provider: Mapped[Provider] = relationship()
    check_logs: Mapped[list["LinkCheckLog"]] = relationship(
        back_populates="share_link", cascade="all, delete-orphan"
    )


class LinkCheckLog(Base):
    __tablename__ = "link_check_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    share_link_id: Mapped[int] = mapped_column(ForeignKey("channel_share_links.id", ondelete="CASCADE"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    result: Mapped[str] = mapped_column(String(30), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(String(500))

    share_link: Mapped[ChannelShareLink] = relationship(back_populates="check_logs")


class LinkClick(Base):
    __tablename__ = "link_clicks"

    id: Mapped[int] = mapped_column(primary_key=True)
    share_link_id: Mapped[int] = mapped_column(ForeignKey("channel_share_links.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"), index=True)
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    referer: Mapped[str | None] = mapped_column(String(1000))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip_hash: Mapped[str | None] = mapped_column(String(64))


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_query: Mapped[str] = mapped_column(String(255))
    normalized_query: Mapped[str] = mapped_column(String(255), index=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    searched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(500))


class ImportBatch(TimestampMixin, Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), default="preview", index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    ready_rows: Mapped[int] = mapped_column(Integer, default=0)
    warning_rows: Mapped[int] = mapped_column(Integer, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    conflict_rows: Mapped[int] = mapped_column(Integer, default=0)
    committed_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    rows: Mapped[list["ImportRawRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="ImportRawRow.row_number"
    )


class ImportRawRow(Base):
    __tablename__ = "import_raw_rows"
    __table_args__ = (UniqueConstraint("batch_id", "row_number", name="uq_import_batch_row"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    parsed_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    row_status: Mapped[str] = mapped_column(String(30), default="ready", index=True)
    message: Mapped[str | None] = mapped_column(String(1000))
    normalized_url_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    matched_resource_id: Mapped[int | None] = mapped_column(ForeignKey("resources.id", ondelete="SET NULL"))

    batch: Mapped[ImportBatch] = relationship(back_populates="rows")


class ImportError(Base):
    __tablename__ = "import_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    row_number: Mapped[int | None] = mapped_column(Integer)
    field_name: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str] = mapped_column(String(100), index=True)
    message: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BackgroundTask(TimestampMixin, Base):
    __tablename__ = "background_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_type: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)


class FriendLink(Base):
    __tablename__ = "friend_links"
    __table_args__ = (
        Index("ix_friend_link_visible_sort", "is_visible", "sort_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    url: Mapped[str] = mapped_column(String(500))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True)


class AdminOperationLog(Base):
    __tablename__ = "admin_operation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(100), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OrganizerToken(TimestampMixin, Base):
    __tablename__ = "organizer_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class OrganizerIdentity(TimestampMixin, Base):
    __tablename__ = "organizer_identities"
    book_id: Mapped[str] = mapped_column(String(35), primary_key=True)
    resource_id: Mapped[int] = mapped_column(ForeignKey("resources.id", ondelete="CASCADE"), unique=True)
    epub_sha256: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))


class OrganizerBatch(TimestampMixin, Base):
    __tablename__ = "organizer_batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_id: Mapped[int] = mapped_column(ForeignKey("organizer_tokens.id", ondelete="CASCADE"))
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    preview: Mapped[list] = mapped_column(JSON)
    receipt: Mapped[dict] = mapped_column(JSON, default=dict)


class CategoryMapping(TimestampMixin, Base):
    __tablename__ = "category_mappings"
    __table_args__ = (UniqueConstraint("source_main", "source_sub", name="uq_category_mapping_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_main: Mapped[str] = mapped_column(String(100))
    source_sub: Mapped[str] = mapped_column(String(100), default="")
    target_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    target: Mapped[Category] = relationship()


class CategoryRedirect(Base):
    """保留合并前分类及旧网址，不删除历史分类记录。"""
    __tablename__ = "category_redirects"
    source_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"), primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
