from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import false, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models import BackgroundTask, ChannelShareLink, Provider, ResourceChannel
from app.models.base import utcnow
from app.services.links import check_link
from app.services.operations import due_cutoff, monitor_config


logger = logging.getLogger(__name__)
LINK_CHECK_TASK_TYPE = "link_check_batch"


@dataclass(slots=True)
class MonitorResult:
    checked: int = 0
    ok: int = 0
    invalid: int = 0
    errors: int = 0


def queue_link_check_task(
    db: Session,
    link_ids: list[int],
    *,
    admin_id: int | None = None,
    scope: str = "selected",
) -> tuple[BackgroundTask | None, int]:
    """把人工批量检测加入队列，并跳过其他未完成任务中已有的链接。"""
    unique_ids = list(dict.fromkeys(int(value) for value in link_ids if int(value) > 0))[:500]
    active_tasks = list(
        db.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == LINK_CHECK_TASK_TYPE,
                BackgroundTask.status.in_(["pending", "running"]),
            )
        )
    )
    busy_ids = {
        int(value)
        for item in active_tasks
        for value in item.payload.get("link_ids", [])
        if str(value).isdigit()
    }
    pending_ids = [link_id for link_id in unique_ids if link_id not in busy_ids]
    skipped = len(unique_ids) - len(pending_ids)
    if not pending_ids:
        return None, skipped
    task = BackgroundTask(
        task_type=LINK_CHECK_TASK_TYPE,
        status="pending",
        payload={
            "link_ids": pending_ids,
            "total": len(pending_ids),
            "scope": scope,
            "created_by_id": admin_id,
        },
        result={"total": len(pending_ids), "checked": 0, "ok": 0, "invalid": 0, "errors": 0},
    )
    db.add(task)
    db.flush()
    return task, skipped


def recent_link_check_tasks(db: Session, limit: int = 5) -> list[BackgroundTask]:
    return list(
        db.scalars(
            select(BackgroundTask)
            .where(BackgroundTask.task_type == LINK_CHECK_TASK_TYPE)
            .order_by(BackgroundTask.id.desc())
            .limit(limit)
        )
    )


def process_link_check_task(
    db: Session,
    task_id: int,
    *,
    client: httpx.Client | None = None,
) -> MonitorResult:
    """执行一个已入队的批量检测任务；每条完成后提交进度，避免长事务。"""
    task = db.get(BackgroundTask, task_id)
    if not task or task.task_type != LINK_CHECK_TASK_TYPE:
        return MonitorResult()
    if task.status == "pending":
        claimed = db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task.id, BackgroundTask.status == "pending")
            .values(status="running", error_message=None)
        ).rowcount
        if claimed != 1:
            db.rollback()
            return MonitorResult()
        db.commit()
        task = db.get(BackgroundTask, task_id)
    elif task.status != "running":
        return MonitorResult()

    link_ids = [int(value) for value in task.payload.get("link_ids", []) if str(value).isdigit()]
    total = len(link_ids)
    result = MonitorResult()
    task.result = {"total": total, "checked": 0, "ok": 0, "invalid": 0, "errors": 0}
    db.commit()

    settings = get_settings()
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=settings.link_check_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 LinkHealthBot/1.0"},
        )
    try:
        for link_id in link_ids:
            try:
                link = db.scalar(
                    select(ChannelShareLink)
                    .where(ChannelShareLink.id == link_id)
                    .options(selectinload(ChannelShareLink.provider))
                )
                if link is None:
                    result.errors += 1
                else:
                    log = check_link(db, link, client)
                    if log.result == "ok":
                        result.ok += 1
                    elif log.result == "invalid":
                        result.invalid += 1
                    else:
                        result.errors += 1
                result.checked += 1
                task = db.get(BackgroundTask, task_id)
                if task:
                    task.result = {
                        "total": total,
                        "checked": result.checked,
                        "ok": result.ok,
                        "invalid": result.invalid,
                        "errors": result.errors,
                    }
                db.commit()
            except Exception as exc:
                db.rollback()
                result.checked += 1
                result.errors += 1
                task = db.get(BackgroundTask, task_id)
                if task:
                    task.result = {
                        "total": total,
                        "checked": result.checked,
                        "ok": result.ok,
                        "invalid": result.invalid,
                        "errors": result.errors,
                    }
                    task.error_message = f"链接 #{link_id} 检测异常：{exc}"[:1000]
                    db.commit()
                logger.exception("批量任务 #%s 检测链接 #%s 时发生未处理异常", task_id, link_id)
    finally:
        if owned_client:
            client.close()

    task = db.get(BackgroundTask, task_id)
    if task:
        task.status = "completed"
        task.result = {
            "total": total,
            "checked": result.checked,
            "ok": result.ok,
            "invalid": result.invalid,
            "errors": result.errors,
        }
        db.commit()
    return result


def process_next_link_check_task() -> bool:
    """由后台工作线程领取一个人工批量检测任务。"""
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        task = db.scalar(
            select(BackgroundTask)
            .where(
                BackgroundTask.task_type == LINK_CHECK_TASK_TYPE,
                BackgroundTask.status == "pending",
            )
            .order_by(BackgroundTask.id)
        )
        if not task:
            return False
        claimed = db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task.id, BackgroundTask.status == "pending")
            .values(status="running", error_message=None)
        ).rowcount
        if claimed != 1:
            db.rollback()
            return False
        task_id = task.id
        db.commit()

    with SessionLocal() as db:
        process_link_check_task(db, task_id)
    return True


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
            processed = await asyncio.to_thread(process_next_link_check_task)
            if processed:
                await asyncio.sleep(0)
                continue
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
