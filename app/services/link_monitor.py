from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models import ChannelShareLink, Provider, ResourceChannel
from app.models.base import utcnow
from app.services.links import check_link
from app.services.operations import due_cutoff, monitor_config


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MonitorResult:
    checked: int = 0
    ok: int = 0
    invalid: int = 0
    errors: int = 0


def due_link_statement(db: Session, now=None):
    cutoff = due_cutoff(db, now)
    statement = (
        select(ChannelShareLink)
        .join(ChannelShareLink.channel)
        .join(ChannelShareLink.provider)
        .where(
            ResourceChannel.status == "active",
            Provider.status == "active",
            ChannelShareLink.status != "disabled",
        )
        .options(selectinload(ChannelShareLink.provider))
        .order_by(ChannelShareLink.last_checked_at.asc(), ChannelShareLink.id.asc())
    )
    if cutoff is None:
        return statement.where(false())
    return statement.where(or_(ChannelShareLink.last_checked_at.is_(None), ChannelShareLink.last_checked_at <= cutoff))


def due_link_count(db: Session, now=None) -> int:
    return int(db.scalar(select(func.count()).select_from(due_link_statement(db, now).subquery())) or 0)


def check_due_links(
    db: Session,
    *,
    client: httpx.Client | None = None,
    limit: int | None = None,
) -> MonitorResult:
    settings = get_settings()
    config = monitor_config(db)
    links = list(db.scalars(due_link_statement(db).limit(limit or config["batch_size"])).unique())
    result = MonitorResult()
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=settings.link_check_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 LinkHealthBot/1.0"},
        )
    try:
        for link in links:
            try:
                log = check_link(db, link, client)
                db.commit()
                result.checked += 1
                if log.result == "ok":
                    result.ok += 1
                elif log.result == "invalid":
                    result.invalid += 1
                else:
                    result.errors += 1
            except Exception:
                db.rollback()
                result.errors += 1
                logger.exception("自动检测链接 #%s 时发生未处理异常", link.id)
    finally:
        if owned_client:
            client.close()
    return result


def run_monitor_batch() -> MonitorResult:
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        return check_due_links(db)


async def link_monitor_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    while not stop_event.is_set():
        try:
            result = await asyncio.to_thread(run_monitor_batch)
            if result.checked:
                logger.info(
                    "自动链接巡检完成：检测 %s，有效 %s，失效 %s，异常 %s",
                    result.checked,
                    result.ok,
                    result.invalid,
                    result.errors,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("自动链接巡检批次执行失败")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.link_check_poll_seconds)
        except TimeoutError:
            continue
