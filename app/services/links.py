from __future__ import annotations

import hashlib
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models import ChannelShareLink, LinkCheckLog, LinkClick, Provider, ResourceChannel, Resource
from app.models.base import utcnow
from app.providers import registry, url_hash


class DuplicateLinkError(ValueError):
    def __init__(self, existing_link: ChannelShareLink) -> None:
        self.existing_link = existing_link
        super().__init__(f"该链接已被链接 #{existing_link.id} 使用")


def prepare_share_link(db: Session, url: str, extract_code: str | None = None, exclude_id: int | None = None):
    parsed = registry.recognize(url, extract_code)
    digest = url_hash(parsed.normalized_url)
    statement = select(ChannelShareLink).where(ChannelShareLink.normalized_url_hash == digest)
    if exclude_id:
        statement = statement.where(ChannelShareLink.id != exclude_id)
    existing = db.scalar(statement)
    if existing:
        raise DuplicateLinkError(existing)
    provider = db.scalar(select(Provider).where(Provider.code == parsed.provider_code))
    if not provider:
        raise ValueError(f"网盘渠道 {parsed.provider_code} 尚未初始化")
    return parsed, digest, provider


def add_or_replace_link(
    db: Session,
    resource_id: int,
    url: str,
    extract_code: str | None = None,
    link: ChannelShareLink | None = None,
) -> ChannelShareLink:
    parsed, digest, provider = prepare_share_link(db, url, extract_code, link.id if link else None)
    channel = db.scalar(
        select(ResourceChannel).where(
            ResourceChannel.resource_id == resource_id,
            ResourceChannel.provider_id == provider.id,
        )
    )
    if not channel:
        channel = ResourceChannel(resource_id=resource_id, provider_id=provider.id, status="active")
        db.add(channel)
        db.flush()
    if not link:
        link = ChannelShareLink(channel_id=channel.id, provider_id=provider.id)
        db.add(link)
    elif link.channel_id != channel.id:
        link.channel_id = channel.id
    link.provider_id = provider.id
    link.provider_share_id = parsed.share_id
    link.share_url = parsed.normalized_url
    link.normalized_url = parsed.normalized_url
    link.normalized_url_hash = digest
    link.extract_code = parsed.extract_code
    link.status = "pending"
    link.is_visible = False
    link.last_error = None
    db.flush()
    return link


def check_link(db: Session, link: ChannelShareLink, client: httpx.Client | None = None) -> LinkCheckLog:
    adapter = registry.get(link.provider.code if link.provider else db.get(Provider, link.provider_id).code)
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=get_settings().link_check_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 LinkHealthBot/1.0"},
        )
    started = time.perf_counter()
    try:
        response = client.get(link.normalized_url)
        latency = round((time.perf_counter() - started) * 1000)
        body = response.text[:250_000]
        ok, detail = adapter.looks_available(response.status_code, body, str(response.url))
        link.status = "active" if ok else "invalid"
        link.is_visible = ok
        link.last_checked_at = utcnow()
        link.last_error = None if ok else detail
        if ok:
            link.last_ok_at = link.last_checked_at
        log = LinkCheckLog(
            share_link_id=link.id,
            result="ok" if ok else "invalid",
            http_status=response.status_code,
            latency_ms=latency,
            detail=detail,
        )
    except (httpx.HTTPError, OSError) as exc:
        latency = round((time.perf_counter() - started) * 1000)
        threshold = get_settings().link_check_error_threshold
        recent_results = list(
            db.scalars(
                select(LinkCheckLog.result)
                .where(LinkCheckLog.share_link_id == link.id)
                .order_by(LinkCheckLog.checked_at.desc(), LinkCheckLog.id.desc())
                .limit(max(threshold - 1, 0))
            )
        )
        consecutive_errors = 1
        for result in recent_results:
            if result != "error":
                break
            consecutive_errors += 1
        if consecutive_errors >= threshold:
            link.status = "error"
            link.is_visible = False
        link.last_checked_at = utcnow()
        link.last_error = f"连续异常 {consecutive_errors}/{threshold}：{str(exc)}"[:500]
        log = LinkCheckLog(
            share_link_id=link.id,
            result="error",
            latency_ms=latency,
            detail=str(exc)[:500],
        )
    finally:
        if owned_client:
            client.close()
    if log.result == "ok" and link.channel and link.channel.resource:
        # 桌面同步或后台巡检确认链接有效后，资料齐全的草稿可直接发布；
        # 仍有版权、分类或元数据问题的资源继续保留为草稿，进入人工审核队列。
        from app.services.publication import publish_if_ready

        publish_if_ready(link.channel.resource)
    db.add(log)
    db.flush()
    return log


def visible_redirect_link(db: Session, link_id: int) -> ChannelShareLink | None:
    statement = (
        select(ChannelShareLink)
        .join(ChannelShareLink.channel)
        .join(ChannelShareLink.provider)
        .join(Resource, Resource.id == ResourceChannel.resource_id)
        .where(
            ChannelShareLink.id == link_id,
            ChannelShareLink.status == "active",
            ChannelShareLink.is_visible.is_(True),
            ResourceChannel.status == "active",
            Provider.status == "active",
            Resource.publish_status == "published",
        )
        .options(selectinload(ChannelShareLink.channel), selectinload(ChannelShareLink.provider))
    )
    return db.scalar(statement)


def record_click(db: Session, link: ChannelShareLink, referer: str | None, user_agent: str | None, ip: str | None) -> None:
    salt = get_settings().session_secret
    ip_hash = hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest() if ip else None
    db.add(
        LinkClick(
            share_link_id=link.id,
            resource_id=link.channel.resource_id,
            provider_id=link.provider_id,
            referer=(referer or "")[:1000] or None,
            user_agent=(user_agent or "")[:500] or None,
            ip_hash=ip_hash,
        )
    )
