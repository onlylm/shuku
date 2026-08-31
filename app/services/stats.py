from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Category, ChannelShareLink, LinkClick, Resource, SearchQuery, resource_categories
from app.services.resources import visible_resource_query
from app.services.catalog_layout import navigation_categories

# 站点运营所在时区（东八区），用于计算“今日更新”。
SITE_TIMEZONE = timezone(timedelta(hours=8))


@dataclass(slots=True)
class CategoryStat:
    name: str
    slug: str
    count: int


@dataclass(slots=True)
class SiteStats:
    """按前台可见口径统计：已发布且有通过检测的网盘链接。"""

    total_resources: int
    category_count: int
    today_updated: int
    category_counts: list[CategoryStat]


def _visible_resource_ids():
    """前台可见资源的 id 子查询，保证统计与列表页口径一致。"""
    return visible_resource_query().with_only_columns(Resource.id).order_by(None)


def _local_day_start() -> datetime:
    """本地时区当天零点对应的 UTC 时间。"""
    now_local = datetime.now(SITE_TIMEZONE)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def site_stats(db: Session, include_categories: bool = True) -> SiteStats:
    visible_ids = _visible_resource_ids()
    total_resources = int(
        db.scalar(select(func.count()).select_from(visible_ids.subquery())) or 0
    )

    day_start = _local_day_start()
    today_updated = int(
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.id.in_(visible_ids),
                func.coalesce(Resource.published_at, Resource.updated_at) >= day_start,
            )
        )
        or 0
    )

    category_counts: list[CategoryStat] = []
    if include_categories:
        categories = navigation_categories(db)
        for category in categories:
            count = int(
                db.scalar(
                    select(func.count(func.distinct(resource_categories.c.resource_id)))
                    .select_from(resource_categories)
                    .where(
                        resource_categories.c.category_id.in_(select(Category.id).where(
                            (Category.id == category.id) | ((Category.parent_id == category.id) & Category.is_visible.is_(True)))),
                        resource_categories.c.resource_id.in_(visible_ids),
                    )
                )
                or 0
            )
            if count > 0:
                category_counts.append(CategoryStat(category.name, category.slug, count))

    return SiteStats(
        total_resources=total_resources,
        category_count=len(category_counts),
        today_updated=today_updated,
        category_counts=category_counts,
    )


def today_updated_count(db: Session) -> int:
    """只取今日更新数量，供后台仪表盘使用。"""
    return site_stats(db, include_categories=False).today_updated


def today_clicks(db: Session) -> int:
    """今日网盘入口点击数（跳转即视为一次下载意图）。"""
    day_start = _local_day_start()
    return int(
        db.scalar(select(func.count()).select_from(LinkClick).where(LinkClick.clicked_at >= day_start)) or 0
    )


def today_zero_searches(db: Session) -> int:
    """今日无结果搜索次数。"""
    day_start = _local_day_start()
    return int(
        db.scalar(
            select(func.count())
            .select_from(SearchQuery)
            .where(SearchQuery.result_count == 0, SearchQuery.searched_at >= day_start)
        )
        or 0
    )


@dataclass(slots=True)
class DashboardStats:
    """后台仪表盘运营指标。"""

    total_resources: int
    published_resources: int
    today_updated: int
    today_clicks: int
    today_downloads: int
    visible_links: int
    problem_links: int
    total_clicks: int
    zero_searches: int
    today_zero_searches: int


def dashboard_stats(db: Session) -> DashboardStats:
    """汇总后台仪表盘需要的全部指标。"""
    site = site_stats(db, include_categories=False)
    total_resources = int(db.scalar(select(func.count(Resource.id))) or 0)
    published_resources = int(
        db.scalar(select(func.count(Resource.id)).where(Resource.publish_status == "published")) or 0
    )
    visible_links = int(
        db.scalar(
            select(func.count(ChannelShareLink.id)).where(
                ChannelShareLink.status == "active", ChannelShareLink.is_visible.is_(True)
            )
        )
        or 0
    )
    problem_links = int(
        db.scalar(
            select(func.count(ChannelShareLink.id)).where(ChannelShareLink.status.in_(["invalid", "error"]))
        )
        or 0
    )
    total_clicks = int(db.scalar(select(func.count(LinkClick.id))) or 0)
    zero_searches = int(
        db.scalar(select(func.count(SearchQuery.id)).where(SearchQuery.result_count == 0)) or 0
    )
    return DashboardStats(
        total_resources=total_resources,
        published_resources=published_resources,
        today_updated=site.today_updated,
        today_clicks=today_clicks(db),
        today_downloads=today_clicks(db),
        visible_links=visible_links,
        problem_links=problem_links,
        total_clicks=total_clicks,
        zero_searches=zero_searches,
        today_zero_searches=today_zero_searches(db),
    )
