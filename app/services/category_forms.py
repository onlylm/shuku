"""资源编辑页的两级分类选择；兼容旧表单，异常旧分类由运营者确认。"""
from sqlalchemy import select

from app.models import Category, CategoryRedirect
from app.services.catalog_layout import navigation_categories


KEEP_CURRENT = "__keep__"


def category_choices(db):
    redirected = set(db.scalars(select(CategoryRedirect.source_id)))
    roots = [c for c in navigation_categories(db) if c.id not in redirected]
    children = list(db.scalars(select(Category).where(
        Category.parent_id.in_([c.id for c in roots]), Category.is_visible.is_(True)
    ).order_by(Category.sort_order, Category.name, Category.id)))
    return roots, [c for c in children if c.id not in redirected]


def category_picker(db, resource=None, values=None):
    roots, children = category_choices(db)
    root_ids = {str(c.id) for c in roots}
    child_by_id = {str(c.id): c for c in children}
    main, sub, keep = "", "", False
    if values is not None and values.get("category_picker") == "1":
        main = str(values.get("main_category_id") or "")
        sub = str(values.get("subcategory_id") or "")
        if main not in root_ids:
            main = ""
        if sub not in child_by_id or str(child_by_id[sub].parent_id) != main:
            sub = ""
    elif resource and resource.categories:
        selected = {c.id for c in resource.categories}
        selected_roots = [c for c in roots if c.id in selected]
        selected_children = [c for c in children if c.id in selected]
        if (len(selected_roots) == 1 and len(selected_children) <= 1
                and len(selected) == 1 + len(selected_children)
                and all(c.parent_id == selected_roots[0].id for c in selected_children)):
            main = str(selected_roots[0].id)
            sub = str(selected_children[0].id) if selected_children else ""
        else:
            # 多分支、隐藏或已合并分类不默认选第一项，避免仅改书名就悄悄丢分类。
            main, keep = KEEP_CURRENT, True
    return {
        "roots": roots,
        "children": [{"id": c.id, "name": c.name, "parent_id": c.parent_id} for c in children],
        "main": main, "sub": sub, "keep": keep,
    }


def category_ids_from_form(db, form, resource=None):
    if form.get("category_picker") != "1":
        return form.getlist("category_ids")  # 升级前已打开的旧表单仍可提交。
    if len(form.getlist("main_category_id")) > 1 or len(form.getlist("subcategory_id")) > 1:
        raise ValueError("请选择一个一级分类及至多一个对应二级分类")
    main = str(form.get("main_category_id") or "")
    sub = str(form.get("subcategory_id") or "")
    if main == KEEP_CURRENT:
        if resource is None or sub:
            raise ValueError("请重新选择网站分类")
        return [c.id for c in resource.categories]
    if not main:
        if sub:
            raise ValueError("请先选择一级分类")
        return []
    roots, children = category_choices(db)
    root = next((c for c in roots if str(c.id) == main), None)
    if root is None:
        raise ValueError("一级分类已隐藏、合并或不可用，请刷新后重新选择")
    if not sub:
        return [root.id]
    child = next((c for c in children if str(c.id) == sub and c.parent_id == root.id), None)
    if child is None:
        raise ValueError("二级分类不属于所选一级分类，或已不可用，请重新选择")
    return [root.id, child.id]
