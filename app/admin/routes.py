from __future__ import annotations

import mimetypes
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.database import get_db
from app.core.config import get_settings
from app.core.security import (
    authenticate_admin,
    csrf_token,
    current_admin,
    login_rate_limiter,
    session_fingerprint,
    verify_csrf,
)
from app.models import (
    AdminOperationLog,
    BackgroundTask,
    Category,
    CategoryMapping,
    CategoryRedirect,
    ChannelShareLink,
    FriendLink,
    ImportBatch,
    ImportRawRow,
    LinkClick,
    Provider,
    Resource,
    ResourceChannel,
    SearchQuery,
)
from app.models.base import utcnow
from app.services.imports import commit_preview, create_preview
from app.services.links import DuplicateLinkError, add_or_replace_link, check_link
from app.services.metadata_import import MATCH_LABELS, commit_meta_preview, create_meta_preview
from app.services.link_monitor import due_link_count
from app.services.pagination import pagination_context
from app.services.resources import create_resource, update_resource
from app.services.publication import publication_issues
from app.services.category_governance import catalog_audit, merge_categories, merge_preview, save_mapping, move_category_books
from app.services.catalog_layout import fixed_root_ids, layout
from app.services.category_forms import category_picker, category_ids_from_form
from app.services.site_settings import read_value
from app.services.stats import dashboard_stats
from app.services.text import slugify
from app.services.cloud_uploads import (
    CLOUD_TASK_LABELS,
    CloudConnectorError,
    cancel_cloud_task,
    clear_problem_cloud_tasks,
    connector_statuses,
    delete_cloud_task,
    install_quark_connector,
    queue_quark_auth_task,
    queue_upload_tasks,
    retry_cloud_task,
    retry_problem_cloud_tasks,
    scan_local_files,
    upload_progress,
)


router = APIRouter(prefix="/admin")


def _templates(request: Request):
    return request.app.state.templates


def _redirect_login(request: Request):
    return RedirectResponse(f"{request.url_for('admin_login')}?next={request.url.path}", status_code=303)


def _admin_context(request: Request, db: Session, **extra):
    context = {
        "admin": current_admin(request, db),
        "flash": request.session.pop("flash", None),
    }
    context.update(extra)
    return context


def _flash(request: Request, message: str, level: str = "success") -> None:
    request.session["flash"] = {"message": message, "level": level}


def _audit(db: Session, admin_id: int, action: str, entity_type: str, entity_id: object = None, detail=None):
    db.add(
        AdminOperationLog(
            admin_user_id=admin_id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail or {},
        )
    )


def _form_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _valid_category_parent(db: Session, category: Category, parent_id: int | None) -> bool:
    if parent_id is None:
        return True
    parent = db.get(Category, parent_id)
    visited: set[int] = set()
    while parent and parent.id not in visited:
        if parent.id == category.id:
            return False
        visited.add(parent.id)
        parent = parent.parent
    return True


def _cloud_upload_context(request: Request, db: Session, **extra):
    settings = get_settings()
    tasks = list(
        db.scalars(
            select(BackgroundTask)
            .where(
                BackgroundTask.task_type.in_(["cloud_upload", "cloud_auth"]),
                BackgroundTask.status != "completed",
            )
            .order_by(BackgroundTask.id.desc())
            .limit(100)
        )
    )
    context = _admin_context(
        request,
        db,
        active="uploads",
        tasks=tasks,
        task_labels=CLOUD_TASK_LABELS,
        connector_statuses=connector_statuses(settings, db),
        baidu_authorize_url=settings.baidu_authorize_url(),
        allowed_source_roots=settings.upload_source_roots(),
        scan_results=None,
        source_path="",
        scan_limit=20,
        error=None,
        has_active_tasks=any(task.status in {"pending", "running"} for task in tasks),
        progress=upload_progress(db),
    )
    context.update(extra)
    return context


@router.get("/login", name="admin_login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if current_admin(request, db):
        return RedirectResponse(request.url_for("admin_dashboard"), status_code=303)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"csrf": csrf_token(request), "error": None},
    )


@router.post("/login", name="admin_login_submit")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    key = f"{request.client.host if request.client else 'unknown'}:{form.get('username', '')}"
    if not login_rate_limiter.allowed(key):
        error = "登录尝试过多，请稍后再试"
    else:
        user = authenticate_admin(db, str(form.get("username") or ""), str(form.get("password") or ""))
        if user:
            request.session.clear()
            request.session["admin_user_id"] = user.id
            request.session["admin_auth"] = session_fingerprint(user)
            csrf_token(request)
            user.last_login_at = utcnow()
            login_rate_limiter.clear(key)
            db.commit()
            next_url = str(request.query_params.get("next") or "/admin")
            if not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
                next_url = "/admin"
            return RedirectResponse(next_url, status_code=303)
        login_rate_limiter.record_failure(key)
        error = "用户名或密码不正确"
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"csrf": csrf_token(request), "error": error},
        status_code=400,
    )


@router.post("/logout", name="admin_logout")
async def logout(request: Request):
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    request.session.clear()
    return RedirectResponse(request.url_for("admin_login"), status_code=303)


@router.get("", name="admin_dashboard")
def dashboard(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    stats = dashboard_stats(db)
    zero_queries = list(
        db.scalars(
            select(SearchQuery).where(SearchQuery.result_count == 0).order_by(SearchQuery.searched_at.desc()).limit(8)
        )
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context=_admin_context(request, db, stats=stats, zero_queries=zero_queries, active="dashboard"),
    )


@router.get("/resources", name="admin_resources")
def resources_list(
    request: Request,
    q: str = "",
    status: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    if not current_admin(request, db):
        return _redirect_login(request)
    page = max(page, 1)
    per_page = 50
    statement = select(Resource).order_by(Resource.updated_at.desc(), Resource.id.desc())
    if q.strip():
        like = f"%{q.strip()}%"
        statement = statement.where(or_(Resource.title.like(like), Resource.author.like(like), Resource.isbn.like(like)))
    if status:
        statement = statement.where(Resource.publish_status == status)
    total = int(db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0)
    if total and (page - 1) * per_page >= total:
        page = max((total - 1) // per_page + 1, 1)
    resources = list(
        db.scalars(
            statement.options(selectinload(Resource.categories))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    )
    pagination = pagination_context(request.url.path, page, per_page, total, q=q.strip(), status=status)
    recent = list(db.scalars(select(Resource).order_by(Resource.updated_at.desc()).limit(8)))
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/resources.html",
        context=_admin_context(
            request,
            db,
            resources=resources,
            q=q,
            status=status,
            pagination=pagination,
            recent=recent,
            active="resources",
        ),
    )


@router.get("/uploads", name="admin_uploads")
def cloud_upload_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/uploads.html",
        context=_cloud_upload_context(request, db),
    )


@router.post("/uploads/scan", name="admin_upload_scan")
async def cloud_upload_scan(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    source_path = str(form.get("source_path") or "").strip()
    scan_limit = max(1, min(_form_int(form.get("scan_limit"), 20), get_settings().cloud_upload_max_scan_files))
    try:
        candidates = scan_local_files(db, source_path, limit=scan_limit)
        error = None if candidates else "该路径下没有找到 EPUB、MOBI、AZW3、PDF、TXT 或 DOCX 文件"
    except ValueError as exc:
        candidates = []
        error = str(exc)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/uploads.html",
        context=_cloud_upload_context(
            request,
            db,
            scan_results=candidates,
            source_path=source_path,
            scan_limit=scan_limit,
            error=error,
        ),
        status_code=400 if error else 200,
    )


@router.post("/uploads/queue", name="admin_upload_queue")
async def cloud_upload_queue(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        result = queue_upload_tasks(
            db,
            [str(value) for value in form.getlist("selected_path")],
            [str(value) for value in form.getlist("provider_code")],
            auto_create_resource=form.get("auto_create_resource") == "1",
            publish_after_upload=form.get("publish_after_upload") == "1",
            admin_id=admin.id,
        )
        if not result.queued and not result.skipped:
            raise ValueError("请至少选择一个文件")
        _audit(
            db,
            admin.id,
            "queue_cloud_upload",
            "background_task",
            detail={
                "queued": result.queued,
                "skipped": result.skipped,
                "created_resources": result.created_resources,
            },
        )
        db.commit()
        _flash(
            request,
            f"已加入 {result.queued} 个上传任务，跳过 {result.skipped} 个重复或未匹配任务；新建草稿 {result.created_resources} 本",
        )
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.post("/uploads/quark/install", name="admin_quark_install")
async def quark_install(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        path = install_quark_connector()
        _audit(db, admin.id, "install_connector", "provider", "quark", {"path": str(path)})
        db.commit()
        _flash(request, "夸克网盘官方连接器已安装")
    except (CloudConnectorError, OSError) as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.post("/uploads/quark/login", name="admin_quark_login")
async def quark_login(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    status = next(item for item in connector_statuses(get_settings(), db) if item.code == "quark")
    if not status.configured:
        _flash(request, status.message, "danger")
    else:
        task = queue_quark_auth_task(db, admin.id)
        _audit(db, admin.id, "start_connector_auth", "background_task", task.id, {"provider": "quark"})
        db.commit()
        _flash(request, "已启动夸克授权，请在自动打开的浏览器中确认；完成后刷新本页")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.post("/uploads/tasks/{task_id}/retry", name="admin_upload_task_retry")
async def cloud_upload_retry(task_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    task = retry_cloud_task(db, task_id)
    if task:
        _audit(db, admin.id, "retry", "background_task", task.id)
        db.commit()
        _flash(request, "任务已重新加入队列")
    else:
        _flash(request, "上传任务不存在", "danger")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.post("/uploads/tasks/{task_id}/cancel", name="admin_upload_task_cancel")
async def cloud_upload_cancel(task_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    task = cancel_cloud_task(db, task_id)
    if task:
        _audit(db, admin.id, "cancel", "background_task", task.id)
        db.commit()
        _flash(request, "等待中的任务已取消")
    else:
        _flash(request, "上传任务不存在", "danger")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.post("/uploads/tasks/{task_id}/delete", name="admin_upload_task_delete")
async def cloud_upload_delete(task_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    task = delete_cloud_task(db, task_id)
    if task and task.status in {"failed", "needs_auth", "cancelled"}:
        _audit(db, admin.id, "delete", "background_task", task_id)
        db.commit()
        _flash(request, "任务记录已删除；本地文件和网盘文件未受影响")
    elif task:
        _flash(request, "等待中、上传中或已完成的任务不能在这里删除", "danger")
    else:
        _flash(request, "上传任务不存在", "danger")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.post("/uploads/tasks/clear-problem", name="admin_upload_tasks_clear_problem")
async def cloud_upload_clear_problem(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    count = clear_problem_cloud_tasks(db)
    _audit(db, admin.id, "clear_problem", "background_task", detail={"count": count})
    db.commit()
    _flash(request, f"已清理 {count} 条失败、待授权或已取消记录；文件未受影响")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.get("/uploads/logs", name="admin_upload_logs")
def upload_logs(
    request: Request,
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    if not current_admin(request, db):
        return _redirect_login(request)
    page = max(page, 1)
    per_page = 50
    statement = (
        select(BackgroundTask)
        .where(BackgroundTask.task_type == "cloud_upload", BackgroundTask.status == "completed")
        .order_by(BackgroundTask.id.desc())
    )
    if q.strip():
        like = f"%{q.strip()}%"
        statement = statement.where(
            BackgroundTask.payload["file_name"].as_string().like(like)
        )
    total = int(db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0)
    if total and (page - 1) * per_page >= total:
        page = max((total - 1) // per_page + 1, 1)
    tasks = list(
        db.scalars(
            statement.offset((page - 1) * per_page).limit(per_page)
        )
    )
    pagination = pagination_context(request.url.path, page, per_page, total, q=q.strip())
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/upload_logs.html",
        context=_admin_context(
            request,
            db,
            active="uploads",
            tasks=tasks,
            task_labels=CLOUD_TASK_LABELS,
            q=q,
            pagination=pagination,
        ),
    )


@router.post("/uploads/tasks/retry-problem", name="admin_upload_tasks_retry_problem")
async def cloud_upload_retry_problem(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    quark = next(item for item in connector_statuses(get_settings(), db) if item.code == "quark")
    if quark.authorization_state != "authorized":
        _flash(request, "请先完成夸克账号授权，再批量重试", "danger")
        return RedirectResponse(request.url_for("admin_uploads"), status_code=303)
    count = retry_problem_cloud_tasks(db, "quark")
    _audit(db, admin.id, "retry_problem", "background_task", detail={"count": count, "provider": "quark"})
    db.commit()
    _flash(request, f"已将 {count} 个失败任务重新加入队列")
    return RedirectResponse(request.url_for("admin_uploads"), status_code=303)


@router.get("/resources/new", name="admin_resource_new")
def resource_new(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/resource_form.html",
        context=_admin_context(request, db, resource=None, picker=category_picker(db), active="resources", error=None),
    )


@router.post("/resources/new", name="admin_resource_create")
async def resource_create(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    data = dict(form)
    data["metadata_locked"] = form.get("metadata_locked") == "1"
    if form.get("submit_action") == "publish":
        data["publish_status"] = "published"
    data["cover_image"] = await _handle_cover_upload(form)
    try:
        data["category_ids"] = category_ids_from_form(db, form)
        resource = create_resource(db, data)
        share_url = str(form.get("share_url") or "").strip()
        if share_url:
            link = add_or_replace_link(
                db,
                resource.id,
                share_url,
                str(form.get("extract_code") or "").strip() or None,
            )
            if form.get("check_now"):
                check_link(db, link)
            _audit(db, admin.id, "add_link", "share_link", link.id, {"resource_id": resource.id})
        _audit(db, admin.id, "create", "resource", resource.id, {"title": resource.title})
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _templates(request).TemplateResponse(
            request=request,
            name="admin/resource_form.html",
            context=_admin_context(request, db, resource=None, picker=category_picker(db, values=data), active="resources", error=str(exc), values=data),
            status_code=400,
        )
    if data.get("publish_status") == "published" and resource.publish_status != "published":
        _flash(request, "资料已保存为草稿，未发布：" + "；".join(publication_issues(resource)), "warning")
    elif str(form.get("share_url") or "").strip():
        _flash(request, "资源和首个网盘链接已保存；发布状态与链接检测分别管理")
    else:
        _flash(request, "资源已创建；可以继续添加网盘链接")
    return RedirectResponse(request.url_for("admin_resource_edit", resource_id=resource.id), status_code=303)


@router.get("/resources/{resource_id}/edit", name="admin_resource_edit")
def resource_edit(resource_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    resource = db.scalar(
        select(Resource)
        .where(Resource.id == resource_id)
        .options(
            selectinload(Resource.categories),
            selectinload(Resource.channels).selectinload(ResourceChannel.provider),
            selectinload(Resource.channels).selectinload(ResourceChannel.share_links),
        )
    )
    if not resource:
        _flash(request, "资源不存在", "danger")
        return RedirectResponse(request.url_for("admin_resources"), status_code=303)
    providers = list(db.scalars(select(Provider).where(Provider.status != "disabled").order_by(Provider.sort_order)))
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/resource_form.html",
        context=_admin_context(
            request,
            db,
            resource=resource,
            picker=category_picker(db, resource),
            providers=providers,
            active="resources",
            error=None,
            publication_issues=publication_issues(resource),
        ),
    )


@router.post("/resources/{resource_id}/edit", name="admin_resource_update")
async def resource_update(resource_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    resource = db.get(Resource, resource_id)
    if not resource:
        _flash(request, "资源不存在", "danger")
        return RedirectResponse(request.url_for("admin_resources"), status_code=303)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    data = dict(form)
    data["metadata_locked"] = form.get("metadata_locked") == "1"
    if form.get("submit_action") == "publish":
        data["publish_status"] = "published"
    data["cover_image"] = await _handle_cover_upload(form, resource.cover_image)
    try:
        data["category_ids"] = category_ids_from_form(db, form, resource)
        update_resource(db, resource, data)
        _audit(db, admin.id, "update", "resource", resource.id, {"title": resource.title})
        db.commit()
        if data.get("publish_status") == "published" and resource.publish_status != "published":
            _flash(request, "资料已保存为草稿，未发布：" + "；".join(publication_issues(resource)), "warning")
        else:
            _flash(request, "资源信息已保存；书籍网址保持不变")
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_resource_edit", resource_id=resource_id), status_code=303)


@router.post("/resources/{resource_id}/archive", name="admin_resource_archive")
async def resource_archive(resource_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    resource = db.get(Resource, resource_id)
    if resource:
        resource.publish_status = "archived"
        for channel in resource.channels:
            for link in channel.share_links:
                link.is_visible = False
        _audit(db, admin.id, "archive", "resource", resource_id)
        db.commit()
        _flash(request, "资源已归档，所有入口已从前台隐藏")
    return RedirectResponse(request.url_for("admin_resources"), status_code=303)


_ALLOWED_COVER_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


async def _handle_cover_upload(form, current_cover: str | None = None) -> str | None:
    cover_file = form.get("cover_file")
    cover_url = str(form.get("cover_image") or "").strip()
    if cover_file and getattr(cover_file, "filename", ""):
        content_type = getattr(cover_file, "content_type", "") or mimetypes.guess_type(cover_file.filename)[0] or ""
        content_type = content_type.lower()
        if content_type not in _ALLOWED_COVER_TYPES:
            raise ValueError("封面图片仅支持 jpg、png、webp、gif")
        content = await cover_file.read()
        if len(content) > 5 * 1024 * 1024:
            raise ValueError("封面图片不能超过 5MB")
        suffix = Path(cover_file.filename).suffix.lower()
        ext = {".jpeg": ".jpg", ".jpg": ".jpg", ".png": ".png", ".webp": ".webp", ".gif": ".gif"}.get(suffix, ".jpg")
        covers_dir = Path("app/static/covers")
        covers_dir.mkdir(parents=True, exist_ok=True)
        filename = f"cover-{secrets.token_hex(8)}{ext}"
        (covers_dir / filename).write_bytes(content)
        return f"/static/covers/{filename}"
    if cover_url:
        return cover_url
    return current_cover


def _delete_resource(db: Session, resource: Resource, admin_id: int) -> None:
    """删除单个资源及其关联数据（点击记录、导入匹配、网盘入口）。"""
    _audit(db, admin_id, "delete", "resource", resource.id, {"title": resource.title})
    db.execute(delete(LinkClick).where(LinkClick.resource_id == resource.id))
    db.execute(
        update(ImportRawRow)
        .where(ImportRawRow.matched_resource_id == resource.id)
        .values(matched_resource_id=None)
    )
    db.delete(resource)


@router.post("/resources/{resource_id}/delete", name="admin_resource_delete")
async def resource_delete(resource_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    resource = db.scalar(
        select(Resource)
        .where(Resource.id == resource_id)
        .options(
            selectinload(Resource.channels)
            .selectinload(ResourceChannel.share_links)
            .selectinload(ChannelShareLink.check_logs),
            selectinload(Resource.files),
            selectinload(Resource.categories),
        )
    )
    if not resource:
        _flash(request, "资源不存在", "danger")
    else:
        title = resource.title
        _delete_resource(db, resource, admin.id)
        db.commit()
        _flash(request, f"资源《{title}》及其网盘入口已永久删除")
    return RedirectResponse(request.url_for("admin_resources"), status_code=303)


@router.post("/resources/batch-delete", name="admin_resources_batch_delete")
async def resources_batch_delete(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    selected_ids = {int(value) for value in form.getlist("selected_resource") if str(value).isdigit()}
    if not selected_ids:
        _flash(request, "请至少选择一条资源", "warning")
        return RedirectResponse(request.url_for("admin_resources"), status_code=303)
    resources = list(
        db.scalars(
            select(Resource)
            .where(Resource.id.in_(selected_ids))
            .options(
                selectinload(Resource.channels)
                .selectinload(ResourceChannel.share_links)
                .selectinload(ChannelShareLink.check_logs),
                selectinload(Resource.files),
                selectinload(Resource.categories),
            )
        )
    )
    deleted_count = 0
    for resource in resources:
        _delete_resource(db, resource, admin.id)
        deleted_count += 1
    db.commit()
    missing = len(selected_ids) - deleted_count
    message = f"已删除 {deleted_count} 条资源及其网盘入口"
    if missing:
        message += f"；{missing} 条资源未找到或已被删除"
    _flash(request, message)
    return RedirectResponse(request.url_for("admin_resources"), status_code=303)


@router.post("/resources/{resource_id}/links", name="admin_link_add")
async def link_add(resource_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        link = add_or_replace_link(
            db,
            resource_id,
            str(form.get("share_url") or ""),
            str(form.get("extract_code") or "") or None,
        )
        if form.get("check_now"):
            check_link(db, link)
        _audit(db, admin.id, "add_link", "share_link", link.id, {"resource_id": resource_id})
        db.commit()
        _flash(request, "链接已保存并检测；检测有效时会自动前台显示")
    except (ValueError, DuplicateLinkError) as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_resource_edit", resource_id=resource_id), status_code=303)


@router.post("/links/{link_id}/replace", name="admin_link_replace")
async def link_replace(link_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    link = db.scalar(
        select(ChannelShareLink)
        .where(ChannelShareLink.id == link_id)
        .options(selectinload(ChannelShareLink.channel), selectinload(ChannelShareLink.provider))
    )
    if not link:
        _flash(request, "链接不存在", "danger")
        return RedirectResponse(request.url_for("admin_links"), status_code=303)
    resource_id = link.channel.resource_id
    try:
        add_or_replace_link(
            db,
            resource_id,
            str(form.get("share_url") or ""),
            str(form.get("extract_code") or "") or None,
            link,
        )
        db.flush()
        check_link(db, link)
        _audit(db, admin.id, "replace_link", "share_link", link.id)
        db.commit()
        if link.status == "active":
            _flash(request, "新链接检测有效，已自动恢复前台显示")
        else:
            _flash(request, "新链接仍未通过检测，继续保持前台隐藏", "warning")
    except (ValueError, DuplicateLinkError) as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_resource_edit", resource_id=resource_id), status_code=303)


@router.post("/links/{link_id}/check", name="admin_link_check")
async def link_check(link_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    link = db.scalar(
        select(ChannelShareLink).where(ChannelShareLink.id == link_id).options(selectinload(ChannelShareLink.provider))
    )
    if link:
        log = check_link(db, link)
        _audit(db, admin.id, "check_link", "share_link", link.id, {"result": log.result})
        db.commit()
        if log.result == "ok":
            _flash(request, "检测完成：链接有效并显示")
        elif log.result == "invalid":
            _flash(request, "检测确认链接已失效，已从前台隐藏", "warning")
        elif link.is_visible:
            _flash(request, "本次检测出现网络异常，尚未达到隐藏阈值，前台暂时保留", "warning")
        else:
            _flash(request, "链接连续检测异常，已从前台隐藏", "warning")
    return RedirectResponse(request.headers.get("referer") or request.url_for("admin_links"), status_code=303)


@router.post("/links/{link_id}/hide", name="admin_link_hide")
async def link_hide(link_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    link = db.get(ChannelShareLink, link_id)
    if link:
        link.is_visible = False
        link.status = "disabled"
        _audit(db, admin.id, "hide_link", "share_link", link.id)
        db.commit()
        _flash(request, "链接已手动隐藏")
    return RedirectResponse(request.headers.get("referer") or request.url_for("admin_links"), status_code=303)


@router.get("/links", name="admin_links")
def links_list(
    request: Request,
    status: str = "",
    visibility: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    if not current_admin(request, db):
        return _redirect_login(request)
    page = max(page, 1)
    per_page = 50
    statement = select(ChannelShareLink).order_by(ChannelShareLink.updated_at.desc(), ChannelShareLink.id.desc())
    if status == "problem":
        statement = statement.where(ChannelShareLink.status.in_(["invalid", "error"]))
    elif status:
        statement = statement.where(ChannelShareLink.status == status)
    if visibility == "visible":
        statement = statement.where(ChannelShareLink.is_visible.is_(True))
    elif visibility == "hidden":
        statement = statement.where(ChannelShareLink.is_visible.is_(False))
    total = int(db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0)
    if total and (page - 1) * per_page >= total:
        page = max((total - 1) // per_page + 1, 1)
    links = list(
        db.scalars(
            statement.options(
                selectinload(ChannelShareLink.provider),
                selectinload(ChannelShareLink.channel).selectinload(ResourceChannel.resource),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    )
    pagination = pagination_context(
        request.url.path,
        page,
        per_page,
        total,
        status=status,
        visibility=visibility,
    )
    settings = get_settings()
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/links.html",
        context=_admin_context(
            request,
            db,
            links=links,
            status=status,
            visibility=visibility,
            pagination=pagination,
            monitor={
                "enabled": settings.link_check_automatic_enabled,
                "interval_minutes": settings.link_check_interval_minutes,
                "batch_size": settings.link_check_batch_size,
                "error_threshold": settings.link_check_error_threshold,
                "due_count": due_link_count(db),
            },
            active="links",
        ),
    )


@router.get("/analytics", name="admin_analytics")
def analytics_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    clicks = list(
        db.execute(
            select(LinkClick, Resource, Provider)
            .join(Resource, Resource.id == LinkClick.resource_id)
            .join(Provider, Provider.id == LinkClick.provider_id)
            .order_by(LinkClick.clicked_at.desc())
            .limit(200)
        ).all()
    )
    searches = list(db.scalars(select(SearchQuery).order_by(SearchQuery.searched_at.desc()).limit(200)))
    popular_resources = list(
        db.execute(
            select(Resource.title, func.count(LinkClick.id).label("click_count"))
            .join(LinkClick, LinkClick.resource_id == Resource.id)
            .group_by(Resource.id, Resource.title)
            .order_by(func.count(LinkClick.id).desc())
            .limit(20)
        ).all()
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/analytics.html",
        context=_admin_context(
            request,
            db,
            clicks=clicks,
            searches=searches,
            popular_resources=popular_resources,
            active="analytics",
        ),
    )


META_BATCH_STATUSES = ("meta_preview", "meta_committed", "meta_partial")


@router.get("/import", name="admin_import")
def import_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    batches = list(
        db.scalars(
            select(ImportBatch)
            .where(ImportBatch.status.notin_(META_BATCH_STATUSES))
            .order_by(ImportBatch.created_at.desc())
            .limit(20)
        )
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/import_upload.html",
        context=_admin_context(request, db, batches=batches, active="import"),
    )


@router.get("/import/meta", name="admin_meta_import")
def meta_import_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    batches = list(
        db.scalars(
            select(ImportBatch)
            .where(ImportBatch.status.in_(META_BATCH_STATUSES))
            .order_by(ImportBatch.created_at.desc())
            .limit(20)
        )
    )
    total = int(db.scalar(select(func.count(Resource.id))) or 0)
    missing_isbn = int(db.scalar(select(func.count(Resource.id)).where(or_(Resource.isbn.is_(None), Resource.isbn == ""))) or 0)
    missing_category = int(db.scalar(select(func.count(Resource.id)).where(~Resource.categories.any())) or 0)
    category_count = int(db.scalar(select(func.count(Category.id))) or 0)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/meta_import.html",
        context=_admin_context(
            request,
            db,
            batches=batches,
            active="import",
            meta_stats={
                "total": total,
                "missing_isbn": missing_isbn,
                "missing_category": missing_category,
                "categories": category_count,
            },
        ),
    )


@router.post("/import/meta", name="admin_meta_import_upload")
async def meta_import_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        content = await file.read()
        batch = create_meta_preview(db, Path(file.filename or "metadata.xlsx").name, content, admin.id)
        _flash(request, "已按 ISBN / 书名匹配到已有图书，请核对字段与分类后确认导入")
        return RedirectResponse(request.url_for("admin_meta_import_preview", batch_id=batch.id), status_code=303)
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
        return RedirectResponse(request.url_for("admin_meta_import"), status_code=303)


@router.get("/import/meta/{batch_id}", name="admin_meta_import_preview")
def meta_import_preview(batch_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    batch = db.scalar(
        select(ImportBatch)
        .where(ImportBatch.id == batch_id, ImportBatch.status.in_(META_BATCH_STATUSES))
        .options(selectinload(ImportBatch.rows))
    )
    if not batch:
        _flash(request, "补全批次不存在", "danger")
        return RedirectResponse(request.url_for("admin_meta_import"), status_code=303)
    # 大书单里绝大多数行是「网站还没有这本书」，单独折叠，主表只留能处理的行
    actionable = [row for row in batch.rows if row.row_status in {"ready", "warning", "noop", "committed"}]
    unmatched = [row for row in batch.rows if row.row_status not in {"ready", "warning", "noop", "committed"}]
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/meta_import_preview.html",
        context=_admin_context(
            request,
            db,
            batch=batch,
            active="import",
            match_labels=MATCH_LABELS,
            actionable_rows=actionable,
            unmatched_rows=unmatched,
        ),
    )


@router.post("/import/meta/{batch_id}/commit", name="admin_meta_import_commit")
async def meta_import_commit(batch_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    batch = db.scalar(
        select(ImportBatch)
        .where(ImportBatch.id == batch_id, ImportBatch.status.in_(META_BATCH_STATUSES))
        .options(selectinload(ImportBatch.rows))
    )
    if not batch:
        _flash(request, "补全批次不存在", "danger")
        return RedirectResponse(request.url_for("admin_meta_import"), status_code=303)
    selected = {int(value) for value in form.getlist("selected_row") if str(value).isdigit()}
    overwrite = form.get("overwrite") == "1"
    category_mode = "replace"
    try:
        result = commit_meta_preview(
            db, batch, selected, overwrite=overwrite, category_mode=category_mode
        )
        _audit(
            db,
            admin.id,
            "commit_meta_import",
            "import_batch",
            batch.id,
            {"updated": result.updated, "created_categories": result.created_categories},
        )
        db.commit()
        _flash(
            request,
            f"已补全 {result.updated} 本图书，新建分类 {result.created_categories} 个，跳过 {result.skipped} 行",
        )
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_meta_import_preview", batch_id=batch_id), status_code=303)


@router.post("/import", name="admin_import_upload")
async def import_upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        content = await file.read()
        batch = create_preview(db, Path(file.filename or "upload.xlsx").name, content, admin.id)
        _flash(request, "表格解析完成，请检查重复、冲突和疑似匹配后再确认导入")
        return RedirectResponse(request.url_for("admin_import_preview", batch_id=batch.id), status_code=303)
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
        return RedirectResponse(request.url_for("admin_import"), status_code=303)


@router.get("/import/{batch_id}", name="admin_import_preview")
def import_preview(batch_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    batch = db.scalar(
        select(ImportBatch).where(ImportBatch.id == batch_id).options(selectinload(ImportBatch.rows))
    )
    if not batch:
        _flash(request, "导入批次不存在", "danger")
        return RedirectResponse(request.url_for("admin_import"), status_code=303)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/import_preview.html",
        context=_admin_context(request, db, batch=batch, active="import"),
    )


@router.post("/import/{batch_id}/commit", name="admin_import_commit")
async def import_commit(batch_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    batch = db.scalar(
        select(ImportBatch).where(ImportBatch.id == batch_id).options(selectinload(ImportBatch.rows))
    )
    if not batch:
        _flash(request, "导入批次不存在", "danger")
        return RedirectResponse(request.url_for("admin_import"), status_code=303)
    selected = {int(value) for value in form.getlist("selected_row") if str(value).isdigit()}
    try:
        result = commit_preview(db, batch, selected)
        _audit(db, admin.id, "commit_import", "import_batch", batch.id, {"committed": result.committed})
        db.commit()
        _flash(request, f"已导入 {result.committed} 条，跳过 {result.skipped} 条；新资源为草稿，请在资源管理核验后发布")
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_import_preview", batch_id=batch_id), status_code=303)


@router.get("/categories", name="admin_categories")
def categories_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    categories = list(
        db.scalars(
            select(Category)
            .options(
                selectinload(Category.parent),
                selectinload(Category.children),
                selectinload(Category.resources),
            )
            .order_by(Category.sort_order, Category.name)
        )
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/categories.html",
        context=_admin_context(request, db, categories=categories, active="categories", fixed_roots=fixed_root_ids(db), migration_report=read_value(db, "catalog_upgrade_v1")),
    )


@router.get("/categories/governance", name="admin_category_governance")
def category_governance_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    return _templates(request).TemplateResponse(request=request, name="admin/category_governance.html",
        context=_admin_context(request, db, active="categories", audit=catalog_audit(db),
            categories=list(db.scalars(select(Category).options(selectinload(Category.parent)).where(Category.is_visible.is_(True)).order_by(Category.parent_id, Category.sort_order, Category.id))),
            mappings=list(db.scalars(select(CategoryMapping).options(selectinload(CategoryMapping.target)).order_by(CategoryMapping.source_main, CategoryMapping.source_sub)))))


@router.post("/categories/mapping", name="admin_category_mapping")
async def category_mapping_save(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        mapping = save_mapping(db, form.get("source_main"), form.get("source_sub"), _form_int(form.get("target_id")))
        _audit(db, admin.id, "category_mapping", "category_mapping", mapping.id,
               {"source_main": mapping.source_main, "source_sub": mapping.source_sub, "target_id": mapping.target_id})
        db.commit()
        _flash(request, "映射已保存；已有图书不会悄悄改类，请重新预检同步或编辑待处理图书")
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_category_governance"), status_code=303)


@router.post("/categories/merge-preview", name="admin_category_merge_preview")
async def category_merge_preview(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        preview = merge_preview(db, _form_int(form.get("source_id")), _form_int(form.get("target_id")))
        return _templates(request).TemplateResponse(request=request, name="admin/category_merge_preview.html",
            context=_admin_context(request, db, active="categories", preview=preview))
    except ValueError as exc:
        _flash(request, str(exc), "danger")
        return RedirectResponse(request.url_for("admin_category_governance"), status_code=303)


@router.post("/categories/merge", name="admin_category_merge")
async def category_merge_confirm(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    try:
        log = merge_categories(db, _form_int(form.get("source_id")), _form_int(form.get("target_id")),
                               str(form.get("fingerprint") or ""), admin.id)
        db.commit()
        _flash(request, f"已合并 {len(log.detail['rows'])} 本图书的分类；旧分类记录及网址已保留，审计编号 {log.id}")
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "danger")
    return RedirectResponse(request.url_for("admin_category_governance"), status_code=303)


@router.post("/categories", name="admin_category_create")
async def category_create(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    name = str(form.get("name") or "").strip()
    if not name:
        _flash(request, "分类名称不能为空", "danger")
    else:
        slug = slugify(str(form.get("slug") or name))
        if db.scalar(select(Category.id).where(Category.slug == slug)):
            _flash(request, "分类别名已存在", "danger")
        else:
            parent_id = _form_int(form.get("parent_id"), 0) or None
            parent = db.get(Category, parent_id) if parent_id else None
            if layout(db) and parent_id not in fixed_root_ids(db):
                _flash(request, "顶部导航已固定，请选择一个导航大类作为上级，新增二级分类", "danger")
                return RedirectResponse(request.url_for("admin_categories"), status_code=303)
            if parent_id and (not parent or parent.parent_id is not None or db.get(CategoryRedirect, parent.id)):
                _flash(request, "上级必须是有效一级分类；网站只维护两级分类", "danger")
                return RedirectResponse(request.url_for("admin_categories"), status_code=303)
            if db.scalar(select(Category.id).where(Category.name == name, Category.parent_id == parent_id)):
                _flash(request, "同一上级下已有同名分类，请复用或先合并", "danger")
                return RedirectResponse(request.url_for("admin_categories"), status_code=303)
            category = Category(
                name=name,
                slug=slug,
                description=str(form.get("description") or "").strip() or None,
                parent_id=parent_id,
                sort_order=_form_int(form.get("sort_order"), 0),
                is_visible=form.get("is_visible") == "1",
            )
            db.add(category)
            db.flush()
            _audit(db, admin.id, "create", "category", category.id)
            db.commit()
            _flash(request, "分类已添加")
    return RedirectResponse(request.url_for("admin_categories"), status_code=303)


@router.get("/categories/{category_id}/edit", name="admin_category_edit")
def category_edit(category_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    category = db.get(Category, category_id)
    if not category:
        _flash(request, "分类不存在", "danger")
        return RedirectResponse(request.url_for("admin_categories"), status_code=303)
    categories = list(
        db.scalars(select(Category).where(Category.id != category_id).order_by(Category.sort_order, Category.name))
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/category_form.html",
        context=_admin_context(
            request,
            db,
            category=category,
            categories=categories,
            fixed_roots=fixed_root_ids(db),
            active="categories",
            error=None,
        ),
    )


@router.post("/categories/{category_id}/edit", name="admin_category_update")
async def category_update(category_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    category = db.get(Category, category_id)
    if not category:
        _flash(request, "分类不存在", "danger")
        return RedirectResponse(request.url_for("admin_categories"), status_code=303)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    name = str(form.get("name") or "").strip()
    slug = slugify(str(form.get("slug") or name))
    parent_id = _form_int(form.get("parent_id"), 0) or None
    if not name:
        _flash(request, "分类名称不能为空", "danger")
    elif db.scalar(select(Category.id).where(Category.slug == slug, Category.id != category.id)):
        _flash(request, "分类网址别名已存在", "danger")
    elif db.get(CategoryRedirect, category.id):
        _flash(request, "已合并分类保留作旧网址跳转，请编辑目标分类", "danger")
    elif category.id in fixed_root_ids(db) and parent_id is not None:
        _flash(request, "固定导航大类不能移为二级分类", "danger")
    elif layout(db) and parent_id != category.parent_id and parent_id not in fixed_root_ids(db):
        _flash(request, "请选择一个固定导航大类作为新上级", "danger")
    elif db.scalar(select(Category.id).where(Category.name == name, Category.parent_id == parent_id, Category.id != category.id)):
        _flash(request, "同一上级下已有同名分类，请先合并", "danger")
    elif parent_id and (not db.get(Category, parent_id) or db.get(Category, parent_id).parent_id is not None or category.children or db.get(CategoryRedirect, parent_id)):
        _flash(request, "只允许两级分类；有下级的分类不能移为二级", "danger")
    elif not _valid_category_parent(db, category, parent_id):
        _flash(request, "不能把分类移动到自己的下级分类中", "danger")
    else:
        moved_books = move_category_books(db, category, parent_id)
        category.name = name
        category.slug = slug
        category.description = str(form.get("description") or "").strip() or None
        category.parent_id = parent_id
        category.sort_order = _form_int(form.get("sort_order"), 0)
        category.is_visible = form.get("is_visible") == "1"
        _audit(db, admin.id, "update", "category", category.id, {"name": category.name, "moved_books": moved_books})
        db.commit()
        _flash(request, "分类设置已保存")
    return RedirectResponse(request.url_for("admin_category_edit", category_id=category_id), status_code=303)


@router.post("/categories/{category_id}/delete", name="admin_category_delete")
async def category_delete(category_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    category = db.scalar(
        select(Category)
        .where(Category.id == category_id)
        .options(selectinload(Category.children), selectinload(Category.resources))
    )
    if not category:
        _flash(request, "分类不存在", "danger")
    elif category.id in fixed_root_ids(db):
        _flash(request, "固定导航大类不能删除；可编辑名称和下级分类", "danger")
    elif category.children:
        _flash(request, "该分类还有下级分类，请先移动或删除下级分类", "danger")
    elif category.resources:
        _flash(request, "该分类仍有关联资源，请先移动资源；也可以直接隐藏分类", "danger")
    elif db.scalar(select(CategoryMapping.id).where(CategoryMapping.target_id == category.id)) or db.scalar(select(CategoryRedirect.source_id).where(or_(CategoryRedirect.source_id == category.id, CategoryRedirect.target_id == category.id))):
        _flash(request, "该分类被来源映射或旧网址引用，不能删除", "danger")
    else:
        _audit(db, admin.id, "delete", "category", category.id, {"name": category.name})
        db.delete(category)
        db.commit()
        _flash(request, "空分类已删除")
    return RedirectResponse(request.url_for("admin_categories"), status_code=303)


@router.post("/categories/{category_id}/toggle", name="admin_category_toggle")
async def category_toggle(category_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    category = db.get(Category, category_id)
    if category and db.get(CategoryRedirect, category.id):
        _flash(request, "已合并的旧分类不能重新显示，请编辑目标分类", "danger")
        return RedirectResponse(request.url_for("admin_categories"), status_code=303)
    if category:
        category.is_visible = not category.is_visible
        _audit(db, admin.id, "toggle", "category", category.id, {"is_visible": category.is_visible})
        db.commit()
    return RedirectResponse(request.url_for("admin_categories"), status_code=303)


@router.get("/providers", name="admin_providers")
def providers_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    providers = list(db.scalars(select(Provider).order_by(Provider.sort_order, Provider.id)))
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/providers.html",
        context=_admin_context(request, db, providers=providers, active="providers"),
    )


@router.post("/providers/{provider_id}/toggle", name="admin_provider_toggle")
async def provider_toggle(provider_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    provider = db.get(Provider, provider_id)
    if provider:
        provider.status = "disabled" if provider.status == "active" else "active"
        if provider.status == "disabled":
            link_ids = select(ResourceChannel.id).where(ResourceChannel.provider_id == provider.id)
            for link in db.scalars(select(ChannelShareLink).where(ChannelShareLink.channel_id.in_(link_ids))):
                link.is_visible = False
        _audit(db, admin.id, "toggle", "provider", provider.id, {"status": provider.status})
        db.commit()
    return RedirectResponse(request.url_for("admin_providers"), status_code=303)


@router.get("/friend-links", name="admin_friend_links")
def friend_links_page(request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    links = list(
        db.scalars(select(FriendLink).order_by(FriendLink.sort_order, FriendLink.id))
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/friend_links.html",
        context=_admin_context(request, db, links=links, active="friend_links"),
    )


@router.post("/friend-links", name="admin_friend_link_create")
async def friend_link_create(request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    name = str(form.get("name") or "").strip()
    url = str(form.get("url") or "").strip()
    if not name or not url:
        _flash(request, "名称和链接不能为空", "danger")
    elif not url.startswith(("http://", "https://")):
        _flash(request, "链接必须以 http:// 或 https:// 开头", "danger")
    else:
        link = FriendLink(
            name=name,
            url=url,
            sort_order=_form_int(form.get("sort_order"), 0),
            is_visible=form.get("is_visible") == "1",
        )
        db.add(link)
        _audit(db, admin.id, "create", "friend_link", link.id, {"name": link.name})
        db.commit()
        _flash(request, "友情链接已添加")
    return RedirectResponse(request.url_for("admin_friend_links"), status_code=303)


@router.get("/friend-links/{link_id}/edit", name="admin_friend_link_edit")
def friend_link_edit(link_id: int, request: Request, db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    link = db.get(FriendLink, link_id)
    if not link:
        _flash(request, "友情链接不存在", "danger")
        return RedirectResponse(request.url_for("admin_friend_links"), status_code=303)
    return _templates(request).TemplateResponse(
        request=request,
        name="admin/friend_link_form.html",
        context=_admin_context(request, db, link=link, active="friend_links", error=None),
    )


@router.post("/friend-links/{link_id}/edit", name="admin_friend_link_update")
async def friend_link_update(link_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    link = db.get(FriendLink, link_id)
    if not link:
        _flash(request, "友情链接不存在", "danger")
        return RedirectResponse(request.url_for("admin_friend_links"), status_code=303)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    name = str(form.get("name") or "").strip()
    url = str(form.get("url") or "").strip()
    if not name or not url:
        _flash(request, "名称和链接不能为空", "danger")
    elif not url.startswith(("http://", "https://")):
        _flash(request, "链接必须以 http:// 或 https:// 开头", "danger")
    else:
        link.name = name
        link.url = url
        link.sort_order = _form_int(form.get("sort_order"), 0)
        link.is_visible = form.get("is_visible") == "1"
        _audit(db, admin.id, "update", "friend_link", link.id, {"name": link.name})
        db.commit()
        _flash(request, "友情链接已保存")
    return RedirectResponse(request.url_for("admin_friend_links"), status_code=303)


@router.post("/friend-links/{link_id}/delete", name="admin_friend_link_delete")
async def friend_link_delete(link_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    link = db.get(FriendLink, link_id)
    if link:
        _audit(db, admin.id, "delete", "friend_link", link.id, {"name": link.name})
        db.delete(link)
        db.commit()
        _flash(request, "友情链接已删除")
    return RedirectResponse(request.url_for("admin_friend_links"), status_code=303)


@router.post("/friend-links/{link_id}/toggle", name="admin_friend_link_toggle")
async def friend_link_toggle(link_id: int, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form()
    verify_csrf(request, str(form.get("csrf_token") or ""))
    link = db.get(FriendLink, link_id)
    if link:
        link.is_visible = not link.is_visible
        _audit(db, admin.id, "toggle", "friend_link", link.id, {"is_visible": link.is_visible})
        db.commit()
    return RedirectResponse(request.url_for("admin_friend_links"), status_code=303)
