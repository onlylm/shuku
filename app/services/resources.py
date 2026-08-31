from __future__ import annotations

import secrets
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models import Category, ChannelShareLink, Provider, Resource, ResourceChannel
from app.services.text import clean_isbn, normalize_title, slugify
from app.services.publication import apply_publication_gate


def resource_public_url(resource_id: int) -> str:
    """公开详情页只使用本站配置域名和不可变的数据库编号。"""
    return f"{get_settings().public_base_url.rstrip('/')}/book/id/{resource_id}"


def unique_slug(db: Session, title: str, current_id: int | None = None) -> str:
    base = slugify(title)[:220]
    candidate = base
    index = 2
    while True:
        query = select(Resource.id).where(Resource.slug == candidate)
        if current_id:
            query = query.where(Resource.id != current_id)
        if db.scalar(query) is None:
            return candidate
        candidate = f"{base}-{index}"
        index += 1


def next_resource_code() -> str:
    return f"BK-{datetime.now():%Y%m%d}-{secrets.token_hex(3).upper()}"


def create_resource(db: Session, data: dict[str, object]) -> Resource:
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("书名不能为空")
    resource = Resource(
        resource_code=next_resource_code(),
        resource_type=str(data.get("resource_type") or "book"),
        title=title,
        normalized_title=normalize_title(title),
        slug=unique_slug(db, str(data.get("slug") or title)),
        subtitle=_optional(data.get("subtitle")),
        author=_optional(data.get("author")),
        translator=_optional(data.get("translator")),
        publisher=_optional(data.get("publisher")),
        isbn=clean_isbn(_optional(data.get("isbn"))),
        language=str(data.get("language") or "zh-CN"),
        publish_year=_int_or_none(data.get("publish_year")),
        description=_optional(data.get("description")),
        formats=_optional(data.get("formats")),
        cover_image=_optional(data.get("cover_image")),
        copyright_status=str(data.get("copyright_status") or "pending"),
        source_reference=_optional(data.get("source_reference")),
        publish_status=str(data.get("publish_status") or "draft"),
        seo_title=_optional(data.get("seo_title")),
        seo_description=_optional(data.get("seo_description")),
        metadata_locked=bool(data.get("metadata_locked", False)),
    )
    category_ids = [int(value) for value in data.get("category_ids", []) if str(value).isdigit()]
    if category_ids:
        resource.categories = list(db.scalars(select(Category).where(Category.id.in_(category_ids))))
    apply_publication_gate(resource)
    db.add(resource)
    db.flush()
    return resource


def update_resource(db: Session, resource: Resource, data: dict[str, object]) -> Resource:
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("书名不能为空")
    resource.title = title
    resource.normalized_title = normalize_title(title)
    # ID 网址不随书名变化；旧 slug 也保留，用于兼容历史链接。
    for field in (
        "resource_type",
        "subtitle",
        "author",
        "translator",
        "publisher",
        "language",
        "description",
        "formats",
        "copyright_status",
        "source_reference",
        "seo_title",
        "seo_description",
        "cover_image",
    ):
        if field in data:
            value = data[field]
            setattr(resource, field, str(value).strip() if value not in {None, ""} else None)
    resource.resource_type = resource.resource_type or "book"
    resource.language = resource.language or "zh-CN"
    resource.copyright_status = resource.copyright_status or "pending"
    if "metadata_locked" in data:
        resource.metadata_locked = bool(data["metadata_locked"])
    if "isbn" in data:
        resource.isbn = clean_isbn(_optional(data.get("isbn")))
    if "publish_year" in data:
        resource.publish_year = _int_or_none(data.get("publish_year"))
    if "publish_status" in data:
        resource.publish_status = str(data.get("publish_status") or "draft")
    if "category_ids" in data:
        category_ids = [int(value) for value in data.get("category_ids", []) if str(value).isdigit()]
        resource.categories = list(db.scalars(select(Category).where(Category.id.in_(category_ids)))) if category_ids else []
    apply_publication_gate(resource)
    db.flush()
    return resource


def visible_resource_query():
    return (
        select(Resource)
        .where(Resource.publish_status == "published")
        .options(selectinload(Resource.categories), selectinload(Resource.channels))
        .distinct()
    )


def search_resource_statement(query: str):
    normalized = normalize_title(query)
    if not normalized:
        return None
    like = f"%{normalized}%"
    return visible_resource_query().where(
        or_(
            Resource.normalized_title.like(like),
            func.lower(func.coalesce(Resource.author, "")).like(like),
            Resource.isbn.like(f"%{query.strip()}%"),
        )
    )


def search_resources(db: Session, query: str, limit: int = 60, offset: int = 0) -> list[Resource]:
    statement = search_resource_statement(query)
    if statement is None:
        return []
    return list(
        db.scalars(
            statement.order_by(Resource.published_at.desc(), Resource.id.desc()).offset(offset).limit(limit)
        ).unique()
    )


def get_visible_links(db: Session, resource_id: int) -> list[ChannelShareLink]:
    statement = (
        select(ChannelShareLink)
        .join(ChannelShareLink.channel)
        .join(ChannelShareLink.provider)
        .where(
            ResourceChannel.resource_id == resource_id,
            ResourceChannel.status == "active",
            Provider.status == "active",
            ChannelShareLink.status == "active",
            ChannelShareLink.is_visible.is_(True),
        )
        .options(selectinload(ChannelShareLink.provider))
        .order_by(ChannelShareLink.priority, Provider.sort_order)
    )
    return list(db.scalars(statement))


def _optional(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip()) if value not in {None, ""} else None
    except ValueError:
        return None
