"""固定导航与受控的来源分类路由；预检不写入分类，提交才创建二级目录。"""
import hashlib
from types import SimpleNamespace

from sqlalchemy import select

from app.catalog_v1 import GROUPS, LAYOUT_KEY, normalized_name, source_group
from app.models import Category, CategoryRedirect, SiteSetting
from app.services.publication import POLLUTION


def layout(db):
    row = db.get(SiteSetting, LAYOUT_KEY)
    return row.value if row else {}


def navigation_categories(db):
    saved = layout(db)
    query = select(Category).where(Category.is_visible.is_(True), Category.parent_id.is_(None))
    if not saved:
        return list(db.scalars(query.order_by(Category.sort_order, Category.id)))
    ordered_ids = [saved.get("roots", {}).get(g[0]) for g in GROUPS]
    found = {c.id: c for c in db.scalars(query.where(Category.id.in_([x for x in ordered_ids if x])))}
    return [found[cid] for cid in ordered_ids if cid in found]


def fixed_root_ids(db):
    return set(layout(db).get("roots", {}).values())


def auto_path(db, main, sub, *, allow_planned=False):
    saved = layout(db)
    group = source_group(main)
    if not saved or not group:
        raise ValueError(f"分类“{main} / {sub}”尚未映射，请在分类管理中确认")
    root = db.get(Category, saved.get("roots", {}).get(group[0]))
    if not root or not root.is_visible or root.parent_id is not None or db.get(CategoryRedirect, root.id):
        raise ValueError("对应导航大类不可用，请在分类管理中检查")
    leaf = str(sub or ("" if group[1] else main)).strip()
    if not leaf or normalized_name(leaf) == normalized_name(root.name):
        return [root]
    if len(leaf) > 100 or any(ord(c) < 32 for c in leaf) or POLLUTION.search(leaf) or normalized_name(leaf) in {"未知", "未识别", "未分类", "待分类", "其他"}:
        raise ValueError("二级分类无效或尚未确认，请人工检查")
    children = list(db.scalars(select(Category).where(Category.parent_id == root.id)))
    matches = [c for c in children if normalized_name(c.name) == normalized_name(leaf)]
    if len(matches) > 1:
        visible = [c for c in matches if c.is_visible and not db.get(CategoryRedirect, c.id)]
        matches = visible if len(visible) == 1 else matches
    if len(matches) == 1:
        if not matches[0].is_visible or db.get(CategoryRedirect, matches[0].id):
            raise ValueError("对应二级分类已隐藏，请人工确认，不自动重新显示")
        return [root, matches[0]]
    if matches or not allow_planned:
        raise ValueError("该二级分类尚未建立，请通过桌面同步预检创建，或在后台确认")
    return [root, SimpleNamespace(id=None, name=leaf, parent_id=root.id, is_visible=True)]


def plan_data(path):
    return [{"id": c.id, "name": c.name, "parent_id": c.parent_id} for c in path]


def same_plan(current, expected):
    if len(current) != len(expected):
        return False
    return all(
        c.name == old["name"]
        and (old["id"] is None or c.id == old["id"])
        and ("parent_id" not in old or c.parent_id == old["parent_id"])
        for c, old in zip(current, expected)
    )


def materialize(db, path):
    if not path or path[-1].id is not None:
        return path
    root, candidate = path
    # 同大类的创建串行化，重复批次/并发上传不会生成同名二级目录。
    planned_name = root.name
    root = db.scalar(select(Category).where(Category.id == root.id).with_for_update().execution_options(populate_existing=True))
    if not root or not root.is_visible or root.parent_id is not None or root.name != planned_name or db.get(CategoryRedirect, root.id):
        raise ValueError("导航分类在预检后已变化，请重新预检")
    children = list(db.scalars(select(Category).where(Category.parent_id == root.id).with_for_update().execution_options(populate_existing=True)))
    matches = [c for c in children if normalized_name(c.name) == normalized_name(candidate.name)]
    visible = [c for c in matches if c.is_visible and not db.get(CategoryRedirect, c.id)]
    if matches:
        if len(visible) != 1:
            raise ValueError("分类在预检后被隐藏或出现重名，请重新预检")
        return [root, visible[0]]
    base = "topic-" + hashlib.sha256(f"{root.id}:{normalized_name(candidate.name)}".encode()).hexdigest()[:24]
    slug, suffix = base, 2
    while db.scalar(select(Category.id).where(Category.slug == slug)):
        slug, suffix = f"{base}-{suffix}", suffix + 1
    child = Category(name=candidate.name, slug=slug, parent_id=root.id, is_visible=True)
    db.add(child)
    db.flush()
    return [root, child]
