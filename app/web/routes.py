from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.database import get_db
from app.models import Category, CategoryRedirect, ChannelShareLink, FriendLink, Provider, Resource, ResourceChannel
from app.services.category_governance import canonical_category
from app.services.catalog_layout import navigation_categories
from app.services.links import record_click, visible_redirect_link
from app.services.pagination import pagination_context
from app.services.resources import get_visible_links, resource_public_url, visible_resource_query
from app.services.search import search_and_record
from app.services.stats import site_stats


router = APIRouter()


def _templates(request: Request):
    return request.app.state.templates


def _nav_categories(db: Session) -> list[Category]:
    return navigation_categories(db)


def _friend_links(db: Session) -> list[FriendLink]:
    return list(
        db.scalars(
            select(FriendLink)
            .where(FriendLink.is_visible.is_(True))
            .order_by(FriendLink.sort_order, FriendLink.id)
        )
    )


def _page_context(db: Session, **values):
    return {"nav_categories": _nav_categories(db), "friend_links": _friend_links(db), **values}


def _cards_for_resources(db: Session, resources: list[Resource]) -> list[dict[str, object]]:
    if not resources:
        return []
    resource_ids = [item.id for item in resources]
    counts = dict(
        db.execute(
            select(ResourceChannel.resource_id, func.count(ChannelShareLink.id))
            .join(ChannelShareLink, ChannelShareLink.channel_id == ResourceChannel.id)
            .join(Provider, Provider.id == ResourceChannel.provider_id)
            .where(
                ResourceChannel.resource_id.in_(resource_ids),
                ResourceChannel.status == "active",
                Provider.status == "active",
                ChannelShareLink.status == "active",
                ChannelShareLink.is_visible.is_(True),
            )
            .group_by(ResourceChannel.resource_id)
        ).all()
    )
    return [{"resource": item, "link_count": counts.get(item.id, 0)} for item in resources]


def _cards(db: Session, statement) -> list[dict[str, object]]:
    return _cards_for_resources(db, list(db.scalars(statement).unique()))


def _statement_total(db: Session, statement) -> int:
    return int(db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0)


def _validate_page(page: int, per_page: int, total: int) -> None:
    if total and (page - 1) * per_page >= total:
        raise HTTPException(status_code=404, detail="该页不存在")


@router.get("/", name="home")
def home(request: Request, db: Session = Depends(get_db)):
    categories = list(
        db.scalars(select(Category).where(Category.is_visible.is_(True)).order_by(Category.sort_order, Category.id))
    )
    recent = _cards(db, visible_resource_query().order_by(Resource.published_at.desc(), Resource.id.desc()).limit(8))
    return _templates(request).TemplateResponse(
        request=request,
        name="web/index.html",
        context=_page_context(
            db,
            active_nav="home",
            categories=categories,
            cards=recent,
            canonical=str(request.url_for("home")),
            stats=site_stats(db),
        ),
    )


@router.get("/search", name="search")
def search(
    request: Request,
    q: str = Query(default="", max_length=120),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    per_page = 24
    resources, total = (
        search_and_record(db, q, request.headers.get("user-agent"), page=page, per_page=per_page)
        if q.strip()
        else ([], 0)
    )
    _validate_page(page, per_page, total)
    cards = _cards_for_resources(db, resources)
    pagination = pagination_context(request.url.path, page, per_page, total, q=q.strip())
    return _templates(request).TemplateResponse(
        request=request,
        name="web/search.html",
        context=_page_context(
            db,
            q=q.strip(),
            cards=cards,
            pagination=pagination,
            canonical=f"{get_settings().public_base_url}/search?q={quote(q.strip())}",
            seo_robots="noindex,follow" if q.strip() else "index,follow",
        ),
    )


@router.get("/books", name="all_resources")
def all_resources(
    request: Request,
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    per_page = 24
    statement = visible_resource_query().order_by(Resource.published_at.desc(), Resource.id.desc())
    total = _statement_total(db, statement)
    _validate_page(page, per_page, total)
    cards = _cards(db, statement.offset((page - 1) * per_page).limit(per_page))
    pagination = pagination_context(request.url.path, page, per_page, total)
    canonical = str(request.url_for("all_resources")) + (f"?page={page}" if page > 1 else "")
    return _templates(request).TemplateResponse(
        request=request,
        name="web/books.html",
        context=_page_context(
            db,
            active_nav="recent",
            cards=cards,
            pagination=pagination,
            canonical=canonical,
        ),
    )


@router.get("/category/{slug}", name="category_detail")
def category_detail(
    slug: str,
    request: Request,
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
):
    category = db.scalar(
        select(Category)
        .where(Category.slug == slug)
        .options(selectinload(Category.parent), selectinload(Category.children))
    )
    if not category:
        raise HTTPException(status_code=404, detail="分类不存在")
    if db.get(CategoryRedirect, category.id):
        target = canonical_category(db, category)
        if not target.is_visible:
            raise HTTPException(status_code=404, detail="分类不存在")
        return RedirectResponse(str(request.url_for("category_detail", slug=target.slug)) + (f"?page={page}" if page > 1 else ""), status_code=301)
    if not category.is_visible:
        raise HTTPException(status_code=404, detail="分类不存在")
    statement = (
        visible_resource_query()
        .join(Resource.categories)
        .where(Category.id.in_(select(Category.id).where(
            (Category.id == category.id) | ((Category.parent_id == category.id) & Category.is_visible.is_(True))
        )))
        .order_by(Resource.published_at.desc())
    )
    per_page = 24
    total = _statement_total(db, statement)
    _validate_page(page, per_page, total)
    cards = _cards(db, statement.offset((page - 1) * per_page).limit(per_page))
    pagination = pagination_context(request.url.path, page, per_page, total)
    canonical = str(request.url_for("category_detail", slug=slug)) + (f"?page={page}" if page > 1 else "")
    active_category = category.parent if category.parent else category
    nav_children = list(db.scalars(select(Category).where(
        Category.parent_id == active_category.id, Category.is_visible.is_(True),
        Category.resources.any(Resource.publish_status == "published"),
    ).order_by(Category.sort_order, Category.id)))
    return _templates(request).TemplateResponse(
        request=request,
        name="web/category.html",
        context=_page_context(
            db,
            active_nav="category",
            active_category=active_category,
            category=category,
            nav_children=nav_children,
            cards=cards,
            pagination=pagination,
            canonical=canonical,
        ),
    )


@router.get("/collections", name="collections")
def collections(request: Request, db: Session = Depends(get_db)):
    categories = list(
        db.scalars(
            select(Category)
            .where(Category.is_visible.is_(True))
            .options(selectinload(Category.resources))
            .order_by(Category.sort_order, Category.id)
        )
    )
    return _templates(request).TemplateResponse(
        request=request,
        name="web/collections.html",
        context=_page_context(
            db,
            active_nav="collections",
            categories=categories,
            canonical=str(request.url_for("collections")),
        ),
    )


@router.get("/disclaimer", name="disclaimer")
def disclaimer(request: Request, db: Session = Depends(get_db)):
    return _templates(request).TemplateResponse(
        request=request,
        name="web/disclaimer.html",
        context=_page_context(db, canonical=str(request.url_for("disclaimer"))),
    )


@router.head("/book/id/{resource_id:int}", name="resource_detail_head", include_in_schema=False)
@router.get("/book/id/{resource_id:int}", name="resource_detail")
def resource_detail(resource_id: int, request: Request, db: Session = Depends(get_db)):
    if not 0 < resource_id <= 2**63 - 1:
        raise HTTPException(status_code=404, detail="资源不存在")
    resource = db.scalar(
        select(Resource)
        .where(Resource.id == resource_id, Resource.publish_status == "published")
        .options(selectinload(Resource.categories))
    )
    if not resource:
        raise HTTPException(status_code=404, detail="资源不存在")
    path = request.url_for("resource_detail", resource_id=resource.id).path
    if request.url.path != path:
        # /book/id/0001 等写法只保留一个规范地址。
        return RedirectResponse(path, status_code=301)
    links = get_visible_links(db, resource.id)
    if request.method == "GET":
        # 旧址跳转、HEAD 检测不计浏览量；浏览不改变内容更新时间。
        db.execute(update(Resource).where(Resource.id == resource.id).values(view_count=Resource.view_count + 1, updated_at=Resource.updated_at))
        db.commit()
    response = _templates(request).TemplateResponse(
        request=request,
        name="web/book.html",
        context=_page_context(
            db,
            resource=resource,
            links=links,
            canonical=resource_public_url(resource.id),
        ),
    )
    if request.method == "HEAD":
        response.body = b""
    return response


@router.api_route("/book/{slug}", methods=["GET", "HEAD"], name="legacy_resource_detail", include_in_schema=False)
def legacy_resource_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    # 即使 slug 是纯数字，也只按旧书名别名查找，不把它解释成图书 ID。
    resource_id = db.scalar(select(Resource.id).where(Resource.slug == slug, Resource.publish_status == "published"))
    if resource_id is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    path = request.url_for("resource_detail", resource_id=resource_id).path
    return RedirectResponse(path, status_code=301)


@router.get("/go/{link_id}", name="go_link")
def go_link(link_id: int, request: Request, db: Session = Depends(get_db)):
    link = visible_redirect_link(db, link_id)
    if not link:
        return _templates(request).TemplateResponse(
            request=request,
            name="web/link_unavailable.html",
            context=_page_context(db),
            status_code=410,
        )
    record_click(
        db,
        link,
        request.headers.get("referer"),
        request.headers.get("user-agent"),
        request.client.host if request.client else None,
    )
    db.commit()
    return RedirectResponse(link.share_url, status_code=302)


@router.get("/robots.txt", response_class=PlainTextResponse, name="robots")
def robots():
    base = get_settings().public_base_url.rstrip("/")
    return f"User-agent: *\nAllow: /\nDisallow: /admin\nSitemap: {base}/sitemap.xml\n"


@router.get("/sitemap.xml", name="sitemap")
def sitemap(db: Session = Depends(get_db)):
    base = get_settings().public_base_url.rstrip("/")
    resources = list(db.scalars(visible_resource_query().order_by(Resource.updated_at.desc())).unique())
    categories = list(db.scalars(select(Category).where(Category.is_visible.is_(True))))
    urls = [
        f"<url><loc>{base}/</loc></url>",
        f"<url><loc>{base}/books</loc></url>",
        f"<url><loc>{base}/collections</loc></url>",
        f"<url><loc>{base}/disclaimer</loc></url>",
    ]
    urls.extend(f"<url><loc>{base}/category/{item.slug}</loc></url>" for item in categories)
    urls.extend(f"<url><loc>{resource_public_url(item.id)}</loc></url>" for item in resources)
    body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">" + "".join(urls) + "</urlset>"
    return Response(body, media_type="application/xml")
