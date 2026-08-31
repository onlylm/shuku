"""冻结的目录数据迁移 v1；只改分类、对应关系和迁移报告，不修改图书字段。

由 Alembic 在同一数据库事务中调用。无运行时 ORM 依赖，不读取电子书或外网。
"""
from collections import defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa

from app.catalog_v1 import GROUPS, LAYOUT_KEY, normalized_name, source_group


def migrate(connection):
    metadata = sa.MetaData()
    tables = {name: sa.Table(name, metadata, autoload_with=connection) for name in (
        "categories", "category_mappings", "category_redirects", "resource_categories", "site_settings")}
    cats, mappings, redirects, links, settings = (tables[n] for n in tables)
    if connection.execute(sa.select(settings.c.key).where(settings.c.key == LAYOUT_KEY)).first():
        return
    now = datetime.now(timezone.utc)
    original = {r["id"]: dict(r) for r in connection.execute(sa.select(cats)).mappings()}
    original_redirects = dict(connection.execute(sa.select(redirects.c.source_id, redirects.c.target_id)).all())
    used_slugs = {r["slug"] for r in original.values()}
    report = {"version": 1, "categories": [], "unmapped_category_ids": [], "changed_books": 0, "review_resource_ids": [], "mapping_conflicts": []}

    def ancestry(cid):
        result = []
        while cid in original and cid not in result:
            result.append(cid)
            cid = original[cid]["parent_id"]
        return result if cid is None else []

    def canonical(cid):
        seen = set()
        while cid in original_redirects and cid not in seen:
            seen.add(cid)
            cid = original_redirects[cid]
        return cid if cid not in seen else None

    roots = {}
    for index, (code, name, _broad, _specific) in enumerate(GROUPS, 1):
        candidates = [r for r in original.values() if r["name"] == name and r["parent_id"] is None and r["is_visible"] and r["id"] not in original_redirects]
        if candidates:
            roots[code] = min(r["id"] for r in candidates)
        else:
            slug, suffix = "catalog-" + code, 2
            while slug in used_slugs:
                slug, suffix = f"catalog-{code}-{suffix}", suffix + 1
            used_slugs.add(slug)
            roots[code] = connection.execute(cats.insert().values(name=name, slug=slug, parent_id=None,
                sort_order=index * 10, is_visible=True, created_at=now, updated_at=now)).inserted_primary_key[0]

    destinations = {}
    for cid, row in original.items():
        chain = ancestry(cid)
        if cid in original_redirects or not chain or not all(original[x]["is_visible"] for x in chain):
            continue
        group = source_group(original[chain[-1]]["name"])
        if group:
            destinations[cid] = group[0]
        else:
            report["unmapped_category_ids"].append(cid)

    targets = {}
    child_names = {}
    # 原本已在目标大类下的二级分类优先保留其编号和网址。
    ordered = sorted(destinations, key=lambda cid: (original[cid]["parent_id"] != roots[destinations[cid]], cid))
    for cid in ordered:
        row, code = original[cid], destinations[cid]
        root_id = roots[code]
        root_name = next(g[1] for g in GROUPS if g[0] == code)
        broad = source_group(row["name"])
        if cid == root_id or normalized_name(row["name"]) == normalized_name(root_name) or (row["parent_id"] is None and broad and broad[1]):
            target = root_id
        else:
            key = (root_id, normalized_name(row["name"]))
            target = child_names.setdefault(key, cid)
        targets[cid] = target
        if target != cid:
            connection.execute(cats.update().where(cats.c.id == cid).values(is_visible=False, updated_at=now))
            connection.execute(redirects.insert().values(source_id=cid, target_id=target))
        elif cid != root_id and row["parent_id"] != root_id:
            connection.execute(cats.update().where(cats.c.id == cid).values(parent_id=root_id, updated_at=now))
        report["categories"].append({"id": cid, "name": row["name"], "before_parent": row["parent_id"], "target_id": target, "root_id": root_id})

    def target_id(cid):
        resolved = canonical(cid)
        return targets.get(resolved, resolved)

    current = {r["id"]: dict(r) for r in connection.execute(sa.select(cats)).mappings()}
    grouped = defaultdict(set)
    for rid, cid in connection.execute(sa.select(links.c.resource_id, links.c.category_id)):
        grouped[rid].add(cid)
    for rid, before in grouped.items():
        # 旧一级+旧二级表示一条路径，不是两种不同书目主题。
        ancestors = {ancestor for cid in before for ancestor in ancestry(cid)[1:]}
        leaves = before - ancestors
        after = set()
        for cid in leaves:
            dest = target_id(cid)
            if dest not in current:
                after.add(cid)
                continue
            after.add(dest)
            parent = current[dest]["parent_id"]
            if parent is not None:
                after.add(parent)
        if not after:
            after = before
        missing, obsolete = after - before, before - after
        if missing:
            connection.execute(links.insert(), [{"resource_id": rid, "category_id": cid} for cid in sorted(missing)])
        if obsolete:
            connection.execute(links.delete().where(links.c.resource_id == rid, links.c.category_id.in_(obsolete)))
        if after != before:
            report["changed_books"] += 1
        leaf_count = len([c for c in after if current.get(c, {}).get("parent_id") is not None])
        branch_count = len(after & set(roots.values()))
        if branch_count != 1 or leaf_count > 1 or any(not current.get(c, {}).get("is_visible") for c in after):
            report["review_resource_ids"].append(rid)

    for cid, dest in targets.items():
        row, chain = original[cid], ancestry(cid)
        if len(chain) > 2:
            continue
        source = (original[chain[-1]]["name"], row["name"] if len(chain) > 1 else "")
        # 使用数据库自身的排序规则比对，兼容 MySQL 对大小写等价名称的唯一约束。
        existing = connection.execute(sa.select(mappings.c.target_id).where(
            mappings.c.source_main == source[0], mappings.c.source_sub == source[1])).first()
        if existing:
            if target_id(existing[0]) != dest:
                report["mapping_conflicts"].append({"main": source[0], "sub": source[1]})
            continue  # 已保存的人工映射优先，不覆盖。
        connection.execute(mappings.insert().values(source_main=source[0], source_sub=source[1], target_id=dest, created_at=now, updated_at=now))

    connection.execute(settings.insert(), [
        {"key": LAYOUT_KEY, "value": {"version": 1, "roots": roots}, "created_at": now, "updated_at": now},
        {"key": "catalog_upgrade_v1", "value": report, "created_at": now, "updated_at": now},
    ])
