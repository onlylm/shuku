from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import AdminUser
from app.services.resources import resource_public_url, search_resources


router = APIRouter(prefix="/api/v1", tags=["api"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/ready")
def ready(db: Session = Depends(get_db)):
    try:
        # 同时确认数据库可达且基础迁移已创建表，不泄露连接信息。
        db.execute(select(AdminUser.id).limit(1))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="网站正在准备，请稍后重试") from None
    return {"status": "ready"}


@router.get("/resources/search")
def resource_search(q: str = Query(min_length=1, max_length=120), db: Session = Depends(get_db)):
    resources = search_resources(db, q, limit=20)
    return {
        "items": [
            {
                "id": item.id,
                "title": item.title,
                "author": item.author,
                "slug": item.slug,
                "detail_url": resource_public_url(item.id),
                "formats": item.formats,
            }
            for item in resources
        ],
        "total": len(resources),
    }
