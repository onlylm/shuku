from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Resource, SearchQuery
from app.services.resources import search_resource_statement
from app.services.text import normalize_title


def search_and_record(
    db: Session,
    query: str,
    user_agent: str | None = None,
    *,
    page: int = 1,
    per_page: int = 24,
):
    statement = search_resource_statement(query)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) if statement is not None else 0
    total = int(total or 0)
    results = (
        list(
            db.scalars(
                statement.order_by(Resource.published_at.desc(), Resource.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).unique()
        )
        if statement is not None
        else []
    )
    if query.strip():
        db.add(
            SearchQuery(
                raw_query=query.strip()[:255],
                normalized_query=normalize_title(query)[:255],
                result_count=total,
                user_agent=(user_agent or "")[:500] or None,
            )
        )
        db.commit()
    return results, total
