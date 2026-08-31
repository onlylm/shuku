from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_admin, verify_csrf
from app.models import AdminUser, Category, OrganizerBatch, OrganizerToken
from app.services.organizer_contract import CommitChoices, OrganizerPackage
from app.services.organizer_sync import commit_batch, make_preview

router = APIRouter()


def sync_auth(request: Request, db: Session = Depends(get_db)):
    if get_settings().app_env == "production" and request.url.scheme != "https":
        raise HTTPException(403, "生产环境同步必须使用 HTTPS")
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or len(header) > 250:
        raise HTTPException(401, "需要专用同步授权")
    token_hash = hashlib.sha256(header[7:].encode()).hexdigest()
    token = db.scalar(select(OrganizerToken).where(OrganizerToken.token_hash == token_hash, OrganizerToken.is_active.is_(True)))
    admin = db.get(AdminUser, token.admin_user_id) if token else None
    if not token or not admin or not admin.is_active:
        raise HTTPException(401, "同步授权无效或已撤销")
    return token


async def body_json(request, model):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > get_settings().organizer_max_bytes:
            raise HTTPException(413, "同步数据超过允许大小，请拆分批次")
    try:
        return model.model_validate_json(bytes(payload))
    except ValidationError as exc:
        # 不回显可能含源路径、链接或其他敏感字段的原始输入。
        raise HTTPException(422, [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors(include_input=False)])


def own_batch(db, batch_id, token):
    batch = db.get(OrganizerBatch, batch_id)
    if not batch or batch.token_id != token.id:
        raise HTTPException(404, "预检批次不存在")
    return batch


@router.get("/api/v1/organizer/info")
def info(token=Depends(sync_auth), db: Session = Depends(get_db)):
    settings = get_settings()
    return {"site_id": settings.organizer_site_id, "schema_version": "2.0", "max_books": 500,
            "cover_hosts": settings.organizer_cover_hosts.split(","),
            "categories": [{"id": c.id, "parent_id": c.parent_id, "name": c.name} for c in db.scalars(select(Category).order_by(Category.sort_order, Category.id))]}


@router.post("/api/v1/organizer/preview")
async def preview(request: Request, token=Depends(sync_auth), db: Session = Depends(get_db)):
    package = await body_json(request, OrganizerPackage)
    try:
        batch = await run_in_threadpool(make_preview, db, package, token.id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"export_id": batch.id, "site_id": package.site_id, "rows": batch.preview, "receipt": batch.receipt}


@router.post("/api/v1/organizer/batches/{batch_id}/commit")
async def commit(batch_id: str, request: Request, token=Depends(sync_auth), db: Session = Depends(get_db)):
    choices = await body_json(request, CommitChoices)
    batch = own_batch(db, batch_id, token)
    try:
        return await run_in_threadpool(commit_batch, db, batch, choices, token.admin_user_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(409, "当前批次正在处理或数据库忙，请稍后读取回执并重试")


@router.get("/api/v1/organizer/batches/{batch_id}/receipt")
def receipt(batch_id: str, token=Depends(sync_auth), db: Session = Depends(get_db)):
    batch = own_batch(db, batch_id, token)
    return {"site_id": batch.payload["site_id"], "export_id": batch.id, "items": batch.receipt}


def context(request, db, **extra):
    admin = current_admin(request, db)
    return {"admin": admin, "active": "organizer", "flash": None, "tokens": list(db.scalars(select(OrganizerToken).where(OrganizerToken.admin_user_id == admin.id).order_by(OrganizerToken.id.desc()))), "site_id": get_settings().organizer_site_id, "cover_hosts": get_settings().organizer_cover_hosts or "尚未配置", **extra}


@router.get("/admin/organizer", name="admin_organizer")
def admin_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return RedirectResponse("/admin/login", 303)
    return request.app.state.templates.TemplateResponse(request=request, name="admin/organizer.html", context=context(request, db))


@router.post("/admin/organizer/tokens")
async def token_create(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        raise HTTPException(401, "请先登录")
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    secret = "eo_" + secrets.token_urlsafe(40)
    db.add(OrganizerToken(admin_user_id=admin.id, label=str(form.get("label") or "本地整理软件")[:100], token_hash=hashlib.sha256(secret.encode()).hexdigest(), is_active=True))
    db.commit()
    response = request.app.state.templates.TemplateResponse(request=request, name="admin/organizer.html", context=context(request, db, new_token=secret))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/admin/organizer/tokens/{token_id}/revoke")
async def revoke(token_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        raise HTTPException(401, "请先登录")
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    token = db.get(OrganizerToken, token_id)
    if not token or token.admin_user_id != admin.id:
        raise HTTPException(404)
    token.is_active = False
    db.commit()
    return RedirectResponse("/admin/organizer", 303)
