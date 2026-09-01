from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.models import BackgroundTask, Resource, ResourceChannel
from app.models.base import utcnow
from app.services.publication import publication_readiness_issues


logger = logging.getLogger(__name__)
RESOURCE_STATUS_TASK_TYPE = "resource_batch_status"
RESOURCE_TASK_CHUNK_SIZE = 100


@dataclass(slots=True)
class ResourceBatchResult:
    total: int = 0
    processed: int = 0
    changed: int = 0
    skipped: int = 0
    unchanged: int = 0
    errors: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "processed": self.processed,
            "changed": self.changed,
            "skipped": self.skipped,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "reasons": dict(self.reasons),
        }


def apply_resource_status(resource: Resource, action: str) -> tuple[str, list[str]]:
    if action == "draft":
        if resource.publish_status == "draft":
            return "unchanged", []
        resource.publish_status = "draft"
        return "changed", []
    if action != "publish":
        raise ValueError("不支持的批量操作")

    issues = publication_readiness_issues(resource)
    if issues:
        return "skipped", list(dict.fromkeys(issues))
    if resource.publish_status == "published":
        return "unchanged", []
    resource.publish_status = "published"
    resource.published_at = resource.published_at or utcnow()
    return "changed", []


def queue_resource_status_task(
    db: Session,
    resource_ids: list[int],
    *,
    action: str,
    admin_id: int | None = None,
    scope: str = "filtered",
) -> tuple[BackgroundTask | None, int]:
    if action not in {"publish", "draft"}:
        raise ValueError("必须选择发布或转为草稿")
    unique_ids = list(dict.fromkeys(int(value) for value in resource_ids if int(value) > 0))
    active_tasks = list(
        db.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == RESOURCE_STATUS_TASK_TYPE,
                BackgroundTask.status.in_(["pending", "running"]),
            )
        )
    )
    busy_ids = {
        int(value)
        for task in active_tasks
        for value in task.payload.get("resource_ids", [])
        if str(value).isdigit()
    }
    pending_ids = [resource_id for resource_id in unique_ids if resource_id not in busy_ids]
    skipped_busy = len(unique_ids) - len(pending_ids)
    if not pending_ids:
        return None, skipped_busy
    task = BackgroundTask(
        task_type=RESOURCE_STATUS_TASK_TYPE,
        status="pending",
        payload={
            "resource_ids": pending_ids,
            "total": len(pending_ids),
            "action": action,
            "scope": scope,
            "created_by_id": admin_id,
        },
        result=ResourceBatchResult(total=len(pending_ids)).as_dict(),
    )
    db.add(task)
    db.flush()
    return task, skipped_busy


def recent_resource_tasks(db: Session, limit: int = 3) -> list[BackgroundTask]:
    return list(
        db.scalars(
            select(BackgroundTask)
            .where(BackgroundTask.task_type == RESOURCE_STATUS_TASK_TYPE)
            .order_by(BackgroundTask.id.desc())
            .limit(limit)
        )
    )


def process_resource_status_task(db: Session, task_id: int) -> ResourceBatchResult:
    task = db.get(BackgroundTask, task_id)
    if not task or task.task_type != RESOURCE_STATUS_TASK_TYPE:
        return ResourceBatchResult()
    if task.status == "pending":
        claimed = db.execute(
            update(BackgroundTask)
            .where(BackgroundTask.id == task.id, BackgroundTask.status == "pending")
            .values(status="running", error_message=None)
        ).rowcount
        if claimed != 1:
            db.rollback()
            return ResourceBatchResult()
        db.commit()
        task = db.get(BackgroundTask, task_id)
    elif task.status != "running":
        return ResourceBatchResult()

    action = str(task.payload.get("action") or "")
    resource_ids = [int(value) for value in task.payload.get("resource_ids", []) if str(value).isdigit()]
    result = ResourceBatchResult(total=len(resource_ids))
    task.result = result.as_dict()
    db.commit()

    for start in range(0, len(resource_ids), RESOURCE_TASK_CHUNK_SIZE):
        chunk_ids = resource_ids[start : start + RESOURCE_TASK_CHUNK_SIZE]
        resources = list(
            db.scalars(
                select(Resource)
                .where(Resource.id.in_(chunk_ids))
                .options(
                    selectinload(Resource.categories),
                    selectinload(Resource.channels).selectinload(ResourceChannel.share_links),
                )
            ).unique()
        )
        resource_by_id = {resource.id: resource for resource in resources}
        for resource_id in chunk_ids:
            resource = resource_by_id.get(resource_id)
            if resource is None:
                result.errors += 1
            else:
                try:
                    outcome, issues = apply_resource_status(resource, action)
                    if outcome == "changed":
                        result.changed += 1
                    elif outcome == "skipped":
                        result.skipped += 1
                        for issue in issues:
                            result.reasons[issue] = result.reasons.get(issue, 0) + 1
                    else:
                        result.unchanged += 1
                except Exception as exc:
                    result.errors += 1
                    logger.exception("批量资源任务 #%s 处理资源 #%s 失败", task_id, resource_id)
                    task.error_message = f"资源 #{resource_id} 处理异常：{exc}"[:1000]
            result.processed += 1
        task = db.get(BackgroundTask, task_id)
        if task:
            task.result = result.as_dict()
        db.commit()

    task = db.get(BackgroundTask, task_id)
    if task:
        task.status = "completed"
        task.result = result.as_dict()
        db.commit()
    return result


def process_next_resource_task() -> bool:
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        task = db.scalar(
            select(BackgroundTask)
            .where(
                BackgroundTask.task_type == RESOURCE_STATUS_TASK_TYPE,
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

    try:
        with SessionLocal() as db:
            process_resource_status_task(db, task_id)
    except Exception as exc:
        logger.exception("批量资源任务 #%s 执行失败", task_id)
        with SessionLocal() as db:
            task = db.get(BackgroundTask, task_id)
            if task:
                task.status = "failed"
                task.error_message = f"后台任务异常：{exc}"[:1000]
                db.commit()
    return True


async def resource_task_worker_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        processed = await asyncio.to_thread(process_next_resource_task)
        if processed:
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2)
        except TimeoutError:
            pass
