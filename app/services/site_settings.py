"""站点展示资料：独立于部署密钥，更新后下一次请求即生效。"""
from __future__ import annotations

import io
import re
import secrets
import warnings
from pathlib import Path

from fastapi import Depends, Request
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.models import SiteSetting
from scripts.server_config import hostname

PROFILE_KEY = "profile"
DEFAULT_DESCRIPTION = "收录公版、开放许可与已获授权的图书和教程，提供清晰的网盘获取入口。"
DEFAULT_FOOTER = "整理公版、开放许可或已取得授权的图书与教程。本站仅提供资源索引，不直接存储第三方分享文件。"


def read_value(db: Session, key: str) -> dict:
    row = db.get(SiteSetting, key)
    return dict(row.value) if row else {}


def put_value(db: Session, key: str, value: dict) -> None:
    row = db.get(SiteSetting, key)
    if row is None:
        db.add(SiteSetting(key=key, value=value))
    else:
        row.value = value


def profile(db: Session, config=None) -> dict:
    config = config or get_settings()
    return {
        "name": config.app_name, "description": DEFAULT_DESCRIPTION,
        "footer": DEFAULT_FOOTER, "contact_email": "", "logo": "", "favicon": "",
        **read_value(db, PROFILE_KEY),
    }


def clean_profile(data: dict) -> dict:
    result = {}
    for key, limit in {"name": 60, "description": 300, "footer": 1000, "contact_email": 254}.items():
        value = str(data.get(key, "")).strip()
        if len(value) > limit or "\x00" in value:
            raise ValueError(f"{key} 内容过长或包含不支持的字符")
        result[key] = value
    if not result["name"]:
        raise ValueError("请填写网站名称")
    if result["contact_email"] and not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", result["contact_email"]):
        raise ValueError("联系邮箱格式不正确")
    return result


def parse_hosts(text: str) -> list[str]:
    hosts = list(dict.fromkeys(hostname(h) for h in re.split(r"[,，\s]+", text.strip()) if h))
    if len(hosts) > 20:
        raise ValueError("最多可设置20个域名")
    return hosts


def cover_hosts(db: Session, config=None) -> list[str]:
    config = config or get_settings()
    saved = read_value(db, "sync")
    # 显式清空必须生效，不能又回落到旧环境白名单。
    return saved.get("cover_hosts", [x.strip().lower() for x in config.organizer_cover_hosts.split(",") if x.strip()])


def save_image(payload: bytes, kind: str) -> str:
    if kind not in {"logo", "favicon"} or not payload or len(payload) > 2 * 1024 * 1024:
        raise ValueError("请上传不超过2MB的 PNG、JPEG 或 WebP 图片")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as img:
                if img.format not in {"PNG", "JPEG", "WEBP"} or img.width * img.height > 16_000_000:
                    raise ValueError("图片格式或尺寸不支持；请使用 PNG、JPEG 或 WebP")
                img.load()
                normalized = img.convert("RGBA")
                normalized.thumbnail((512, 512) if kind == "logo" else (64, 64))
                directory = get_settings().local_storage_root / "branding"
                directory.mkdir(parents=True, exist_ok=True)
                name = secrets.token_hex(16) + ".png"
                # 不保留原文件名、SVG脚本、附加元数据或用户提供的文件路径。
                normalized.save(directory / name, format="PNG")
                return "/site-assets/" + name
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("无法读取图片，请换一张有效的 PNG、JPEG 或 WebP") from exc


def bind_profile(request: Request, db: Session = Depends(get_db)):
    if request.url.path in {"/api/v1/health", "/api/v1/ready"}:
        return
    request.state.site_profile = profile(db, request.app.state.config)


def template_profile(request: Request) -> dict:
    config = request.app.state.config
    data = getattr(request.state, "site_profile", {"name": config.app_name, "description": DEFAULT_DESCRIPTION,
        "footer": DEFAULT_FOOTER, "contact_email": "", "logo": "", "favicon": ""})
    return {"app_name": data["name"], "site_profile": data, "server_uploads_available": config.app_env != "production"}
