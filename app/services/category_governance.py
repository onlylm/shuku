"""网站分类是受控目录：只精确匹配或使用管理员明确保存的映射。"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import AdminOperationLog, Category, CategoryMapping, CategoryRedirect, Resource


def canonical_category(db, category):
    seen = set()
    while category:
        if category.id in seen:
            raise ValueError("分类跳转存在循环，请先检查")
        seen.add(category.id)
        redirect = db.get(CategoryRedirect, category.id)
        if not redirect:
            return category
        category = db.get(Category, redirect.target_id)
    raise ValueError("分类不存在")


def category_path(db, target_id):
    target = canonical_category(db, db.get(Category, target_id))
    if not target.is_visible:
        raise ValueError("目标分类已隐藏")
    if target.parent_id is None:
        return [target]
    parent = db.get(Category, target.parent_id)
    if not parent or not parent.is_visible or parent.parent_id is not None or db.get(CategoryRedirect, parent.id):
        raise ValueError("目标必须属于有效的一级／二级分类")
    return [parent, target]


def resolve_categories(db, main, sub=None):
    main, sub = str(main or "").strip(), str(sub or "").strip()
    if not main:
        raise ValueError("缺少网站分类，请在分类管理中选择或建立映射")
    mapping = db.scalar(select(CategoryMapping).where(CategoryMapping.source_main == main, CategoryMapping.source_sub == sub))
    if mapping:
        return category_path(db, mapping.target_id)
    roots = list(db.scalars(select(Category).where(Category.name == main, Category.parent_id.is_(None), Category.is_visible.is_(True))))
    if len(roots) != 1:
        raise ValueError(f"分类“{main}”尚未映射或重名，请在分类管理中确认")
    if not sub:
        return category_path(db, roots[0].id)
    children = list(db.scalars(select(Category).where(Category.name == sub, Category.parent_id == roots[0].id, Category.is_visible.is_(True))))
    if len(children) != 1:
        raise ValueError(f"分类“{main} / {sub}”尚未映射或重名，请在分类管理中确认")
    return category_path(db, children[0].id)


def save_mapping(db, main, sub, target_id):
    main, sub = str(main or "").strip(), str(sub or "").strip()
    if not main or len(main) > 100 or len(sub) > 100:
        raise ValueError("来源一级分类必填，分类名称不能超过100字")
    target = category_path(db, target_id)[-1]
    mapping = db.scalar(select(CategoryMapping).where(CategoryMapping.source_main == main, CategoryMapping.source_sub == sub))
    if mapping:
        mapping.target_id = target.id
    else:
        mapping = CategoryMapping(source_main=main, source_sub=sub, target_id=target.id)
        db.add(mapping)
    db.flush()
    return mapping


def merge_preview(db, source_id, target_id):
    source = db.get(Category, source_id)
    if not source or db.get(CategoryRedirect, source_id):
        raise ValueError("原分类不存在或已合并")
    path = category_path(db, target_id)
    if source.id in {c.id for c in path}:
        raise ValueError("不能合并到自身或自身下级")
    if any(not db.get(CategoryRedirect, c.id) for c in source.children):
        raise ValueError("请先逐个合并下级分类，再合并此一级分类")
    resources = list(db.scalars(select(Resource).where(Resource.categories.any(Category.id == source_id)).options(selectinload(Resource.categories)).order_by(Resource.id)))
    rows = []
    for resource in resources:
        before = sorted(c.id for c in resource.categories)
        after = sorted((set(before) - {source.id, source.parent_id}) | {c.id for c in path})
        rows.append({"id": resource.id, "title": resource.title, "before": before, "after": after,
                     "locked": resource.metadata_locked, "updated_at": resource.updated_at.replace(tzinfo=None).isoformat() if resource.updated_at else None})
    affected_mappings = [{"id": m.id, "target_id": m.target_id} for m in db.scalars(select(CategoryMapping).where(CategoryMapping.target_id == source_id).order_by(CategoryMapping.id))]
    source_main = source.parent.name if source.parent else source.name
    source_sub = source.name if source.parent else ""
    existing = db.scalar(select(CategoryMapping).where(CategoryMapping.source_main == source_main, CategoryMapping.source_sub == source_sub))
    # 同名不同旧分类不能凭名字强行改写已经审核的映射。
    if existing and existing.target_id not in {source_id, path[-1].id}:
        raise ValueError("该来源名称已有其他映射，请先在分类映射中确认")
    payload = {"source_id": source_id, "source_name": source.name, "source_slug": source.slug,
               "source_visible": source.is_visible, "source_parent": source.parent_id,
               "target_id": path[-1].id, "target_name": " / ".join(c.name for c in path),
               "target_path": [(c.id, c.name, c.parent_id) for c in path],
               "rows": rows, "mappings": affected_mappings,
               "source_main": source_main, "source_sub": source_sub,
               "previous_mapping": {"id": existing.id, "target_id": existing.target_id} if existing else None}
    payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return payload


def merge_categories(db, source_id, target_id, expected_fingerprint, admin_id=None):
    # 拿锁后重做预览；并发更改不能使用旧页面确认。
    list(db.scalars(select(Category).where(Category.id.in_([source_id, target_id])).with_for_update().execution_options(populate_existing=True)))
    list(db.scalars(select(Resource).where(Resource.categories.any(Category.id == source_id)).with_for_update().execution_options(populate_existing=True)))
    preview = merge_preview(db, source_id, target_id)
    if preview["fingerprint"] != expected_fingerprint:
        raise ValueError("预览后分类或图书已变化，请重新预览")
    for row in preview["rows"]:
        resource = db.get(Resource, row["id"])
        resource.categories = list(db.scalars(select(Category).where(Category.id.in_(row["after"]))))
        resource.metadata_locked = True
    for row in preview["mappings"]:
        db.get(CategoryMapping, row["id"]).target_id = preview["target_id"]
    db.flush()
    mapping = save_mapping(db, preview["source_main"], preview["source_sub"], preview["target_id"])
    preview["saved_mapping_id"] = mapping.id
    source = db.get(Category, source_id)
    source.is_visible = False
    db.add(CategoryRedirect(source_id=source_id, target_id=preview["target_id"]))
    log = AdminOperationLog(admin_user_id=admin_id, action="merge_categories", entity_type="category",
                            entity_id=str(source_id), detail=preview)
    db.add(log)
    db.flush()
    return log


def catalog_audit(db):
    """只读检查；不按模糊名称自动决定两类是否同义。"""
    from app.services.publication import publication_issues
    grouped = defaultdict(list)
    for c in db.scalars(select(Category).where(Category.is_visible.is_(True)).order_by(Category.id)):
        grouped[(c.parent_id, c.name.strip())].append({"id": c.id, "name": c.name, "slug": c.slug})
    duplicates = [rows for rows in grouped.values() if len(rows) > 1]
    pending, issues = [], []
    resolution_errors = {}
    for r in db.scalars(select(Resource).options(selectinload(Resource.categories)).order_by(Resource.id)):
        problems = publication_issues(r)
        if problems:
            issues.append({"id": r.id, "title": r.title, "publish_status": r.publish_status, "issues": problems})
        if r.source_category_main and not r.metadata_locked:
            key = (r.source_category_main, r.source_category_sub or "")
            if key not in resolution_errors:
                try:
                    resolve_categories(db, *key)
                    resolution_errors[key] = None
                except ValueError as exc:
                    resolution_errors[key] = str(exc)
            if resolution_errors[key]:
                pending.append({"id": r.id, "title": r.title, "main": key[0], "sub": key[1], "reason": resolution_errors[key]})
    return {"duplicate_categories": duplicates, "pending_categories": pending, "publication_issues": issues}
