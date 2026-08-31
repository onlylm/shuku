from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.admin.routes import _admin_context, _audit, _flash, _redirect_login
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import current_admin, hash_password, login_rate_limiter, verify_csrf, verify_password
from app.models import AdminUser
from app.services import maintenance
from app.services.site_settings import clean_profile, cover_hosts, parse_hosts, profile, put_value, read_value, save_image
from scripts.maintenance_protocol import current_version, release_info, version_key
from scripts.server_config import hostname

router = APIRouter()


@router.get("/site-assets/{name}", name="site_asset")
def site_asset(name: str):
    if not re.fullmatch(r"[a-f0-9]{32}\.png", name):
        raise HTTPException(404)
    path = get_settings().local_storage_root / "branding" / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get("/admin/settings", name="admin_settings")
def settings_page(request: Request, tab: str = "site", db: Session = Depends(get_db)):
    if not current_admin(request, db):
        return _redirect_login(request)
    if tab not in {"site", "account", "domains", "sync", "updates"}:
        tab = "site"
    config = get_settings()
    release = read_value(db, "release")
    if release:
        release["available"] = version_key(release["tag"]) > version_key(current_version())
    response = request.app.state.templates.TemplateResponse(request=request, name="admin/settings.html",
        context=_admin_context(request, db, active="settings", tab=tab, profile=profile(db),
            version=current_version(), release=release, control=maintenance.control_status(),
            primary=urlsplit(config.public_base_url).hostname, aliases=config.site_aliases.replace(",", "\n"),
            cover_hosts="\n".join(cover_hosts(db)), domain_draft=read_value(db, "domain_draft")))
    response.headers["Cache-Control"] = "no-store"
    return response


def confirm_password(request, db, form):
    admin = current_admin(request, db)
    if not admin:
        raise HTTPException(401, "请先登录")
    key = f"sensitive:{admin.id}"
    if not login_rate_limiter.allowed(key):
        raise ValueError("密码确认失败次数过多，请10分钟后重试")
    if not verify_password(str(form.get("current_password", "")), admin.password_hash):
        login_rate_limiter.record_failure(key)
        raise ValueError("当前密码不正确")
    login_rate_limiter.clear(key)
    return admin


@router.post("/admin/settings/{section}", name="admin_settings_save")
async def settings_save(section: str, request: Request, db: Session = Depends(get_db)):
    admin = current_admin(request, db)
    if not admin:
        return _redirect_login(request)
    form = await request.form(max_part_size=2 * 1024 * 1024 + 1024)
    verify_csrf(request, str(form.get("csrf_token", "")))
    tab = section if section in {"site", "account", "domains", "sync", "updates"} else "updates"
    try:
        if section == "site":
            data = {**profile(db), **clean_profile(dict(form))}
            for key in ("logo", "favicon"):
                upload = form.get(key)
                if form.get("remove_" + key) == "yes":
                    data[key] = ""
                if getattr(upload, "filename", ""):
                    data[key] = await run_in_threadpool(save_image, await upload.read(2 * 1024 * 1024 + 1), key)
            put_value(db, "profile", data)
            message = "网站资料已保存，前台、登录页和后台立即使用新资料。"
        elif section == "sync":
            put_value(db, "sync", {"cover_hosts": parse_hosts(str(form.get("cover_hosts", "")))})
            message = "封面域名白名单已保存；桌面授权和站点编号保持不变。"
        elif section == "account":
            await run_in_threadpool(confirm_password, request, db, form)
            username = str(form.get("username", "")).strip()
            display_name = str(form.get("display_name", "")).strip()
            password = str(form.get("new_password", ""))
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,63}", username):
                raise ValueError("账号须以字母开头，3—64位，仅含字母、数字、点、下划线或连字符")
            if not 1 <= len(display_name) <= 100:
                raise ValueError("显示名称需为1—100个字符")
            duplicate = db.scalar(select(AdminUser).where(AdminUser.username == username, AdminUser.id != admin.id))
            if duplicate:
                raise ValueError("该账号名称已被使用")
            if password and (not 12 <= len(password) <= 128 or password != str(form.get("confirm_password", ""))):
                raise ValueError("新密码需为12—128个字符，且两次输入一致")
            admin.username, admin.display_name = username, display_name
            if password:
                admin.password_hash = await run_in_threadpool(hash_password, password)
            _audit(db, admin.id, "settings_account", "admin", admin.id, {"password_changed": bool(password)})
            db.commit()
            request.session.clear()
            return RedirectResponse("/admin/login", 303)
        elif section == "domains":
            primary = hostname(str(form.get("primary", "")))
            aliases = [x for x in parse_hosts(str(form.get("aliases", ""))) if x != primary]
            data = {"primary": primary, "aliases": aliases}
            if form.get("apply") == "yes":
                await run_in_threadpool(confirm_password, request, db, form)
                if form.get("confirm") != "yes":
                    raise ValueError("请确认已完成域名解析，并同意保留旧主域名作为访问入口")
                job_id = maintenance.enqueue("domains", {**data, "previous_primary": urlsplit(get_settings().public_base_url).hostname})
                message = "域名验证任务已提交；验证通过才会切换，原主域名将自动保留为别名。任务编号：" + job_id
            else:
                message = "已保存域名草稿，尚未更改实际访问地址。"
            put_value(db, "domain_draft", data)
        elif section == "check-update":
            existing = read_value(db, "release")
            if time.time() - existing.get("checked_at", 0) < 60:
                raise ValueError("刚刚已检查过版本，请一分钟后再试")
            release = await run_in_threadpool(release_info)
            put_value(db, "release", release)
            message = "发现新的正式版本，可查看说明并自行决定是否更新。" if release["available"] else "暂时没有比当前版本更新的正式版本。"
        elif section in {"update", "backup"}:
            await run_in_threadpool(confirm_password, request, db, form)
            if form.get("confirm") != "yes":
                raise ValueError("请勾选操作确认；维护期间网站会暂时不可用")
            payload = {}
            if section == "update":
                release = read_value(db, "release")
                if not release.get("available") or time.time() - release.get("checked_at", 0) > 3600:
                    raise ValueError("请先检查最新正式版本，再确认更新")
                if form.get("tag") != release["tag"] or form.get("sha") != release["sha"]:
                    raise ValueError("版本信息已变化，请刷新后重新确认")
                payload = {"tag": release["tag"], "sha": release["sha"]}
            job_id = maintenance.enqueue(section, payload)
            message = "维护任务已提交；可在系统维护查看结果。任务编号：" + job_id
        else:
            raise HTTPException(404)
        _audit(db, admin.id, "settings_" + section, "site", detail={"section": section})
        db.commit()
        _flash(request, message)
    except ValueError as exc:
        db.rollback()
        _flash(request, str(exc), "error")
    except IntegrityError:
        db.rollback()
        _flash(request, "设置同时被其他操作修改，请刷新后重试；账号名称不能重复。", "error")
    return RedirectResponse("/admin/settings?tab=" + tab, 303)
