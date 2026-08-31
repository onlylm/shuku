from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime

from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.admin.routes import router as admin_router
from app.admin.settings import router as settings_router
from app.api.routes import router as api_router
from app.api.organizer import router as organizer_router
from app.core.config import get_settings
from app.core.security import csrf_token
from app.services.presentation import language_label, provider_label, resource_type_label, status_label
from app.services.link_monitor import link_monitor_loop
from app.services.cloud_uploads import cloud_upload_worker_loop
from app.web.routes import router as web_router
from app.services.site_settings import bind_profile, template_profile
from scripts.maintenance_protocol import current_version


BASE_DIR = Path(__file__).resolve().parent


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = asyncio.Event()
        monitor_task = None
        upload_task = None
        if settings.link_check_automatic_enabled:
            monitor_task = asyncio.create_task(link_monitor_loop(stop_event))
        if settings.cloud_upload_worker_enabled:
            upload_task = asyncio.create_task(cloud_upload_worker_loop(stop_event))
        try:
            yield
        finally:
            stop_event.set()
            if monitor_task:
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task
            if upload_task:
                upload_task.cancel()
                with suppress(asyncio.CancelledError):
                    await upload_task

    app = FastAPI(
        title=settings.app_name,
        version=current_version(),
        dependencies=[Depends(bind_profile)],
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/api/docs" if settings.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env != "production" else None,
    )
    if settings.app_env == "production":
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[urlsplit(settings.public_base_url).hostname, "127.0.0.1", "localhost"],
        )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="jingye_admin",
        max_age=60 * 60 * 8,
        same_site="lax",
        https_only=settings.session_https_only,
    )
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.state.config = settings
    templates = Jinja2Templates(directory=BASE_DIR / "templates", context_processors=[template_profile])

    def _dtformat(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        if not value:
            return "—"
        if value.tzinfo is not None:
            value = value.astimezone()
        return value.strftime(fmt)

    templates.env.filters["dtformat"] = _dtformat
    templates.env.globals.update(
        csrf_token=csrf_token,
        app_name=settings.app_name,
        status_label=status_label,
        provider_label=provider_label,
        resource_type_label=resource_type_label,
        language_label=language_label,
    )
    app.state.templates = templates

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if settings.app_env == "production" and (request.url.path == "/admin/uploads" or request.url.path.startswith("/admin/uploads/")):
            return HTMLResponse("此入口已停用，请通过桌面软件上传并同步网站。", status_code=404)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.exception_handler(404)
    async def not_found(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return HTMLResponse("Not Found", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="web/not_found.html",
            context={"detail": exc.detail if isinstance(exc, HTTPException) else "页面不存在"},
            status_code=404,
        )

    app.include_router(api_router)
    app.include_router(organizer_router)
    app.include_router(admin_router)
    app.include_router(settings_router)
    app.include_router(web_router)
    return app


app = create_app()
