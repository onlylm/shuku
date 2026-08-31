from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Category, ImportBatch, ImportRawRow, Resource
from app.models.base import utcnow
from app.services.imports import parse_upload
from app.services.text import clean_isbn, normalize_title, slugify


"""元数据补全导入。

用途：云盘批量上传只会按文件名建立草稿资源（只有书名、作者、格式），
ISBN、出版社、出版年、简介、分类这些完整的书目数据通常另有一份表格。
本模块把表格按「ISBN → 书名+作者 → 书名精确 → 书名模糊」的顺序映射回
已有的资源，补齐空缺字段，并自动处理分类：

* 表格里写了分类，系统里没有 → 自动新建分类
* 表格里写了分类，系统里已有 → 直接复用（支持同义模糊复用）
* 一本书适合多个分类 → 单元格里用 、,，/|;； 任一分隔符隔开即可
"""


META_HEADERS = {
    # ---------- 标识 ----------
    "isbn": "isbn",
    "ISBN": "isbn",
    "书号": "isbn",
    "国际标准书号": "isbn",
    "文件名": "file_name",
    "文件": "file_name",
    "file": "file_name",
    "file_name": "file_name",
    # ---------- 书名 ----------
    "书名": "title",
    "标题": "title",
    "名称": "title",
    "图书名称": "title",
    "title": "title",
    "副标题": "subtitle",
    "subtitle": "subtitle",
    # ---------- 作者 ----------
    "作者": "author",
    "author": "author",
    "authors": "author",  # Calibre 导出
    "译者": "translator",
    "translator": "translator",
    # ---------- 出版信息 ----------
    "出版社": "publisher",
    "出版方": "publisher",
    "出版": "publisher",
    "publisher": "publisher",
    "出版年份": "publish_year",
    "出版年": "publish_year",
    "出版时间": "publish_year",
    "年份": "publish_year",
    "publish_year": "publish_year",
    "pubdate": "publish_year",  # Calibre 导出：2019-11-01T00:00:00+08:00
    # ---------- 文本 ----------
    "简介": "description",
    "内容简介": "description",
    "描述": "description",
    "description": "description",
    "comments": "description",  # Calibre 导出
    "语言": "language",
    "language": "language",
    "languages": "language",  # Calibre 导出：zho / eng
    "格式": "formats",
    "文件格式": "formats",
    "formats": "formats",
    "封面": "cover_image",
    "封面图": "cover_image",
    "cover_image": "cover_image",
    # ---------- 分类 ----------
    # 单列多值：用 、,，/|;； 隔开即可
    "分类": "categories",
    "分类标签": "categories",
    "类别": "categories",
    "category": "categories",
    "categories": "categories",
    # 两列层级：主分类为一级，子类挂在对应主分类下
    "主分类": "primary_category",
    "一级分类": "primary_category",
    "大类": "primary_category",
    "子类": "sub_category",
    "二级分类": "sub_category",
    "子分类": "sub_category",
    "小类": "sub_category",
    # 注意：Calibre 的 tags 列不做映射。它多是「公众号：xxx」「电子书免费赠送」这类
    # 来源标记而非书目分类，直接灌进来会污染分类体系。请另建「分类」列导入。
}


FIELD_LABELS = {
    "subtitle": "副标题",
    "author": "作者",
    "translator": "译者",
    "publisher": "出版社",
    "publish_year": "出版年",
    "description": "简介",
    "language": "语言",
    "formats": "格式",
    "isbn": "ISBN",
    "cover_image": "封面",
}

# 顺序即写入顺序，ISBN 与出版年需要单独处理
FILLABLE_FIELDS = ("subtitle", "author", "translator", "publisher", "publish_year", "description", "language", "formats", "cover_image")

CATEGORY_SPLIT = re.compile(r"[、,，/／|｜;；]+|\s{2,}")

TITLE_FUZZY_THRESHOLD = 92
CATEGORY_FUZZY_THRESHOLD = 90
MIN_ISBN_LENGTH = 8

# 云盘上传按「书名 - 作者.epub」拆出资源标题，这里做同一套反解，才能对上
FILE_SUFFIX = re.compile(r"\.(epub|mobi|azw3|pdf|txt|docx|zip|rar)$", re.IGNORECASE)
TRAILING_INDEX = re.compile(r"\s*[（(]\d+[)）]$")
TITLE_AUTHOR_SEPARATOR = " - "

LANGUAGE_MAP = {
    "zho": "zh-CN",
    "chi": "zh-CN",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "eng": "en",
    "en": "en",
    "jpn": "ja",
    "ja": "ja",
    "kor": "ko",
    "ko": "ko",
}

FORMAT_SPLIT = re.compile(r"[、,，/／|｜;；·]+")


def split_title_author(value: str) -> tuple[str, str | None]:
    """把「书名 - 作者.epub」还原成 (书名, 作者)，与云盘上传的解析保持一致。"""
    stem = FILE_SUFFIX.sub("", str(value or "").strip())
    stem = TRAILING_INDEX.sub("", stem).strip()
    if TITLE_AUTHOR_SEPARATOR in stem:
        title, author = (part.strip() for part in stem.split(TITLE_AUTHOR_SEPARATOR, 1))
        return title, author or None
    return stem, None


def clean_year(value: object) -> str | None:
    """从 2019-11-01T00:00:00+08:00、2019-11、2019 里取出年份。"""
    if value in {None, ""}:
        return None
    match = re.search(r"(1[89]|20)\d{2}", str(value))
    return match.group(0) if match else None


def clean_language(value: object) -> str | None:
    if value in {None, ""}:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    return LANGUAGE_MAP.get(raw.casefold(), raw)


def clean_formats(value: object) -> str | None:
    if value in {None, ""}:
        return None
    parts = [part.strip().upper() for part in FORMAT_SPLIT.split(str(value)) if part.strip()]
    return " · ".join(dict.fromkeys(parts)) or None


@dataclass(slots=True)
class MetaCommitResult:
    updated: int = 0
    skipped: int = 0
    created_categories: int = 0


@dataclass(slots=True)
class CategoryPlan:
    name: str
    action: str  # existing | matched | created
    target: str
    category_id: int | None = None
    parent: str | None = None
    parent_id: int | None = None
    level: int = 1  # 1 一级（主分类） / 2 二级（子类）


def split_categories(value: str) -> list[str]:
    """拆出多个分类名，去掉序号式的重复并保留书写顺序。"""
    names: list[str] = []
    for part in CATEGORY_SPLIT.split(str(value or "")):
        name = part.strip().strip("·•").strip()
        if not name or name in names:
            continue
        names.append(name)
    return names


def _plan_to_dict(plan: CategoryPlan) -> dict[str, Any]:
    return {
        "name": plan.name,
        "action": plan.action,
        "target": plan.target,
        "category_id": plan.category_id,
        "parent": plan.parent,
        "parent_id": plan.parent_id,
        "level": plan.level,
    }


def unique_category_slug(db: Session, name: str) -> str:
    base = slugify(name)[:100] or "category"
    candidate = base
    index = 2
    while db.scalar(select(Category.id).where(Category.slug == candidate)):
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _load_resources(db: Session) -> tuple[dict[str, Resource], dict[str, Resource], dict[tuple[str, str], Resource], dict[str, list[Resource]]]:
    resources = list(db.scalars(select(Resource)))
    by_isbn: dict[str, Resource] = {}
    by_title: dict[str, Resource] = {}
    by_title_author: dict[tuple[str, str], Resource] = {}
    title_buckets: dict[str, list[Resource]] = {}
    for resource in resources:
        if resource.isbn and len(resource.isbn) >= MIN_ISBN_LENGTH and resource.isbn not in by_isbn:
            by_isbn[resource.isbn] = resource
        title = resource.normalized_title or normalize_title(resource.title)
        by_title.setdefault(title, resource)
        if resource.author:
            by_title_author.setdefault((title, normalize_title(resource.author)), resource)
        key = title[:2] if len(title) >= 2 else title[:1]
        if key:
            title_buckets.setdefault(key, []).append(resource)
    return by_isbn, by_title, by_title_author, title_buckets


def _fuzzy_candidate(
    buckets: dict[str, list[Resource]],
    normalized: str,
) -> tuple[Resource | None, int]:
    if not normalized:
        return None, 0
    for key in (normalized[:2] if len(normalized) >= 2 else "", normalized[:1]):
        candidates = buckets.get(key)
        if not candidates:
            continue
        best = max(candidates, key=lambda item: fuzz.ratio(normalized, item.normalized_title or normalize_title(item.title)))
        score = fuzz.ratio(normalized, best.normalized_title or normalize_title(best.title))
        if score >= TITLE_FUZZY_THRESHOLD:
            return best, score
        if key == normalized[:1]:
            return None, score
    return None, 0


def match_resource(
    db: Session,
    values: dict[str, Any],
    indexes: tuple[dict[str, Resource], dict[str, Resource], dict[tuple[str, str], Resource], dict[str, list[Resource]]],
) -> tuple[Resource | None, str, int | None]:
    by_isbn, by_title, by_title_author, buckets = indexes
    isbn = clean_isbn(values.get("isbn"))
    title = normalize_title(str(values.get("title") or ""))
    author = normalize_title(str(values.get("author") or ""))
    # 表格里的「文件名」和「书名」常常自带「 - 作者」，反解后才是系统里的标题
    file_title, file_author = split_title_author(str(values.get("file_name") or ""))
    book_title, book_author = split_title_author(str(values.get("title") or ""))
    file_title = normalize_title(file_title)
    book_title = normalize_title(book_title)

    def by_pair(candidate_title: str, candidate_author: str | None) -> Resource | None:
        if not candidate_title or not candidate_author:
            return None
        return by_title_author.get((candidate_title, normalize_title(candidate_author)))

    # 1. ISBN 最可靠
    if isbn and len(isbn) >= MIN_ISBN_LENGTH:
        hit = by_isbn.get(isbn)
        if hit:
            return hit, "isbn", None
    # 2. 文件名反解（云盘上传就是按文件名建的，命中率最高）
    hit = by_pair(file_title, file_author)
    if hit:
        return hit, "filename", None
    # 3. 书名单元格自带「 - 作者」
    hit = by_pair(book_title, book_author)
    if hit:
        return hit, "title_author", None
    # 4. 书名 + 作者列
    hit = by_pair(title, author or None)
    if hit:
        return hit, "title_author", None
    # 5. 书名精确
    for candidate_title in (title, book_title, file_title):
        if candidate_title and candidate_title in by_title:
            return by_title[candidate_title], "title", None
    # 6. 书名相似
    for candidate_title in (title, book_title, file_title):
        if candidate_title:
            candidate, score = _fuzzy_candidate(buckets, candidate_title)
            if candidate:
                return candidate, "fuzzy", score
    return None, "none", None


MATCH_LABELS = {
    "isbn": "按 ISBN 精确匹配",
    "filename": "按文件名匹配",
    "title_author": "按书名 + 作者匹配",
    "title": "按书名精确匹配",
    "fuzzy": "书名相似，请确认",
    "none": "未匹配到资源",
}


def _resolve_one(
    name: str,
    parent_name: str | None,
    level: int,
    cache: dict[str, CategoryPlan],
    by_name: dict[str, Category],
    by_slug: dict[str, Category],
) -> CategoryPlan:
    key = f"{parent_name or ''}/{name}"
    cached = cache.get(key)
    if cached:
        return cached

    parent_plan = cache.get(f"/{parent_name}") if parent_name else None
    matches = [category for category in by_name.values() if category.name == name]
    if parent_plan and parent_plan.category_id and len(matches) > 1:
        narrowed = [item for item in matches if item.parent_id == parent_plan.category_id]
        if narrowed:
            matches = narrowed
    existing = matches[0] if matches else None
    if existing is None:
        existing = by_slug.get(slugify(name))
    if existing:
        plan = CategoryPlan(
            name=name,
            action="existing",
            target=existing.name,
            category_id=existing.id,
            parent=parent_name,
            parent_id=existing.parent_id,
            level=level,
        )
    else:
        best: Category | None = None
        best_score = 0
        for category in by_name.values():
            score = fuzz.ratio(name, category.name)
            if score > best_score:
                best, best_score = category, score
        if best and best_score >= CATEGORY_FUZZY_THRESHOLD:
            plan = CategoryPlan(
                name=name,
                action="matched",
                target=best.name,
                category_id=best.id,
                parent=parent_name,
                parent_id=best.parent_id,
                level=level,
            )
        else:
            plan = CategoryPlan(
                name=name,
                action="created",
                target=name,
                parent=parent_name,
                parent_id=parent_plan.category_id if parent_plan else None,
                level=level,
            )
    cache[key] = plan
    return plan


def _resolve_categories(
    db: Session,
    values: dict[str, Any],
    cache: dict[str, CategoryPlan],
    by_name: dict[str, Category],
    by_slug: dict[str, Category],
) -> list[CategoryPlan]:
    """支持三种写法：单列多值「分类」、两列层级「主分类 + 子类」，或两者同时给。"""
    plans: list[CategoryPlan] = []
    primary = str(values.get("primary_category") or "").strip()
    sub = str(values.get("sub_category") or "").strip()

    def push(plan: CategoryPlan) -> None:
        if plan not in plans:
            plans.append(plan)

    if primary:
        push(_resolve_one(primary, None, 1, cache, by_name, by_slug))
    if sub:
        push(_resolve_one(sub, primary or None, 2, cache, by_name, by_slug))
    for name in split_categories(str(values.get("categories") or "")):
        push(_resolve_one(name, None, 1, cache, by_name, by_slug))
    return plans


def _normalize_values(values: dict[str, Any]) -> None:
    """把不同来源的写法统一成系统口径：文件名补齐书名、出版日期取年份、语言与格式标准化。"""
    if not values.get("title") and values.get("file_name"):
        derived, author = split_title_author(str(values["file_name"]))
        if derived:
            values["title"] = derived
            if author and not values.get("author"):
                values["author"] = author
    if values.get("publish_year"):
        values["publish_year"] = clean_year(values["publish_year"]) or ""
    if values.get("language"):
        values["language"] = clean_language(values["language"]) or ""
    if values.get("formats"):
        values["formats"] = clean_formats(values["formats"]) or ""


def _build_fill_plan(resource: Resource, values: dict[str, Any], overwrite: bool) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    for field_name in FILLABLE_FIELDS:
        new_value = str(values.get(field_name) or "").strip()
        if not new_value:
            continue
        current = getattr(resource, field_name, None)
        current_text = "" if current is None else str(current)
        if field_name == "publish_year":
            new_value = str(_int_or_none(new_value) or "")
            if not new_value:
                continue
        if current_text and not overwrite:
            continue
        if current_text == new_value:
            continue
        plan.append(
            {
                "field": field_name,
                "label": FIELD_LABELS[field_name],
                "old": current_text,
                "new": new_value,
            }
        )
    isbn = clean_isbn(values.get("isbn"))
    if isbn and (not resource.isbn or overwrite) and resource.isbn != isbn:
        plan.append({"field": "isbn", "label": "ISBN", "old": resource.isbn or "", "new": isbn})
    return plan


def create_meta_preview(db: Session, filename: str, content: bytes, admin_id: int) -> ImportBatch:
    rows = parse_upload(filename, content)
    limit = get_settings().import_max_rows
    if len(rows) > limit:
        raise ValueError(f"一次最多导入 {limit} 行")

    headers_seen = {_canonical_meta_header(key) for row in rows for key in row}
    headers_seen.discard(None)
    if "title" not in headers_seen and "isbn" not in headers_seen:
        raise ValueError("表格至少需要「书名」或「ISBN」其中一列，否则无法匹配已有资源")

    batch = ImportBatch(
        original_filename=filename[:255],
        created_by_id=admin_id,
        total_rows=len(rows),
        status="meta_preview",
    )
    db.add(batch)
    db.flush()

    indexes = _load_resources(db)
    categories = list(db.scalars(select(Category)))
    by_name = {category.name: category for category in categories}
    by_slug = {category.slug: category for category in categories}
    category_cache: dict[str, CategoryPlan] = {}

    for number, raw in enumerate(rows, start=2):
        values = {_canonical_meta_header(key): _cell(value) for key, value in raw.items()}
        values.pop(None, None)
        _normalize_values(values)
        title = str(values.get("title") or "").strip()
        isbn = clean_isbn(values.get("isbn"))
        resource, match_type, score = match_resource(db, values, indexes)

        if not title and not isbn:
            db.add(
                ImportRawRow(
                    batch_id=batch.id,
                    row_number=number,
                    raw_data=raw,
                    parsed_data={"values": values},
                    row_status="error",
                    message="书名和 ISBN 都为空，无法匹配",
                )
            )
            batch.error_rows += 1
            continue
        if resource is None:
            db.add(
                ImportRawRow(
                    batch_id=batch.id,
                    row_number=number,
                    raw_data=raw,
                    parsed_data={"values": values, "match": {"type": "none"}},
                    row_status="unmatched",
                    message=f"系统里没有找到《{title or isbn}》，可先在资源管理里创建该书",
                )
            )
            batch.error_rows += 1
            continue

        fill = _build_fill_plan(resource, values, overwrite=False)
        category_plans = _resolve_categories(db, values, category_cache, by_name, by_slug)
        existing_names = {category.name for category in resource.categories}
        pending_categories = [plan for plan in category_plans if plan.target not in existing_names]

        if not fill and not pending_categories:
            status = "noop"
            message = "该书字段已完整，无需更新"
        elif match_type == "fuzzy":
            status = "warning"
            message = f"仅按书名相似度 {score}% 匹配到《{resource.title}》，请确认后再导入"
        else:
            status = "ready"
            message = f"{MATCH_LABELS[match_type]}《{resource.title}》"

        if fill:
            message += f"；补齐 {len(fill)} 个字段"
        if pending_categories:
            message += f"；分类 {len(pending_categories)} 个"

        parsed = {
            "values": values,
            "fill": fill,
            "categories": [_plan_to_dict(plan) for plan in category_plans],
            "match": {
                "type": match_type,
                "score": score,
                "resource_id": resource.id,
                "resource_title": resource.title,
            },
        }
        db.add(
            ImportRawRow(
                batch_id=batch.id,
                row_number=number,
                raw_data=raw,
                parsed_data=parsed,
                row_status=status,
                message=message,
                matched_resource_id=resource.id,
            )
        )
        if status == "ready":
            batch.ready_rows += 1
        elif status == "warning":
            batch.warning_rows += 1
        elif status == "noop":
            batch.duplicate_rows += 1
        else:
            batch.error_rows += 1
    db.commit()
    db.refresh(batch)
    return batch


def commit_meta_preview(
    db: Session,
    batch: ImportBatch,
    selected_row_ids: set[int],
    *,
    overwrite: bool = False,
    category_mode: str = "append",
) -> MetaCommitResult:
    if batch.status not in {"meta_preview", "meta_partial"}:
        raise ValueError("该批次已经处理，不能重复提交")
    allowed = {"ready", "warning"} | ({"noop"} if overwrite else set())
    result = MetaCommitResult()
    created_here: set[int] = set()
    for row in batch.rows:
        if row.id not in selected_row_ids or row.row_status not in allowed:
            result.skipped += 1
            continue
        resource = db.get(Resource, row.matched_resource_id) if row.matched_resource_id else None
        if not resource:
            row.row_status = "unmatched"
            row.message = "提交时资源已不存在，已跳过"
            result.skipped += 1
            continue
        parsed = row.parsed_data or {}
        values = parsed.get("values") or {}
        fill = _build_fill_plan(resource, values, overwrite=overwrite) if overwrite else (parsed.get("fill") or [])

        for item in fill:
            field_name = item["field"]
            value: Any = item["new"]
            if field_name == "publish_year":
                resource.publish_year = _int_or_none(value)
            elif field_name == "isbn":
                resource.isbn = clean_isbn(value)
            else:
                setattr(resource, field_name, str(value).strip() or None)

        plans = [CategoryPlan(**plan) for plan in (parsed.get("categories") or [])]
        if plans:
            if category_mode == "replace":
                resource.categories = []
            existing_names = {category.name for category in resource.categories}
            for plan in plans:
                category = None
                if plan.category_id:
                    category = db.get(Category, plan.category_id)
                if category is None:
                    category = db.scalar(select(Category).where(Category.name == plan.name))
                if category is None:
                    category = Category(
                        name=plan.name,
                        slug=unique_category_slug(db, plan.name),
                        is_visible=True,
                    )
                    db.add(category)
                    db.flush()
                    created_here.add(category.id)
                    result.created_categories += 1
                # 子类要挂到对应主分类下，已存在的分类不改动它的归属
                if plan.parent and not category.parent_id and category.id in created_here:
                    parent = db.scalar(select(Category).where(Category.name == plan.parent))
                    if parent and parent.id != category.id:
                        category.parent_id = parent.id
                if category.name not in existing_names:
                    resource.categories.append(category)
                    existing_names.add(category.name)
        row.row_status = "committed"
        result.updated += 1
    batch.status = "meta_committed"
    batch.committed_rows = result.updated
    batch.committed_at = utcnow()
    db.commit()
    return result


def _canonical_meta_header(value: object) -> str | None:
    key = str(value or "").strip()
    if not key:
        return None
    return META_HEADERS.get(key) or META_HEADERS.get(key.casefold())


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int_or_none(value: object) -> int | None:
    if value in {None, ""}:
        return None
    digits = re.sub(r"[^\d]", "", str(value))
    if not digits:
        return None
    try:
        number = int(digits[:4])
    except ValueError:
        return None
    return number or None


__all__ = [
    "MetaCommitResult",
    "commit_meta_preview",
    "create_meta_preview",
    "split_categories",
]
