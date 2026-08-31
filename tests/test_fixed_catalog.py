import copy

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.catalog_v1 import GROUPS
from app.models import Category, CategoryMapping, CategoryRedirect, Resource, SiteSetting
from app.services.catalog_layout import navigation_categories, materialize, plan_data, same_plan
from app.services.category_governance import resolve_categories, save_mapping
from app.services.resources import create_resource
from app.services.stats import site_stats
from scripts.migrate_catalog_v1 import migrate
from tests.test_catalog_governance import sync, next_revision
from tests.test_organizer_sync import auth, package


def upgrade(db):
    db.flush()
    migrate(db.connection())
    db.commit()
    db.expire_all()
    return db.get(SiteSetting, "catalog_layout").value["roots"]


def category(db, name, slug, parent=None, visible=True):
    c = Category(name=name, slug=slug, parent_id=parent.id if parent else None, is_visible=visible)
    db.add(c)
    db.flush()
    return c


def book(db, name, cats, **extra):
    return create_resource(db, {"title": name, "publish_status": "published", "copyright_status": "authorized",
        "source_reference": "合成测试资料", "category_ids": [c.id for c in cats], **extra})


def test_upgrade_keeps_book_facts_old_urls_and_specific_leaf(client, db_session):
    db = db_session
    old = db.get(Category, 1)
    ai = category(db, "人工智能", "old-ai", old)
    lit = category(db, "文学", "old-literature")
    novel = category(db, "小说", "old-novels", lit)
    first = book(db, "合成科技书", [old, ai], metadata_locked=True, isbn="9787020065512", description="原始简介")
    second = book(db, "合成小说", [lit, novel])
    db.commit()
    db.refresh(first); db.refresh(second)
    original = {r.id: {c.name: getattr(r, c.name) for c in Resource.__table__.columns} for r in (first, second)}
    roots = upgrade(db)
    assert len(navigation_categories(db)) == 8
    assert {c.id for c in first.categories} == {roots["science"], ai.id}
    assert {c.id for c in second.categories} == {roots["literature"], novel.id}
    for r in (first, second):
        assert {c.name: getattr(r, c.name) for c in Resource.__table__.columns} == original[r.id]
    assert db.get(Category, old.id).parent_id == roots["science"]
    assert client.get("/category/old-ai").status_code == 200
    response = client.get("/category/old-literature", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"].endswith(db.get(Category, roots["literature"]).slug)
    assert [c.id for c in resolve_categories(db, "编程开发", "人工智能")] == [roots["science"], ai.id]
    assert [c.id for c in resolve_categories(db, "文学", "小说")] == [roots["literature"], novel.id]


def test_unknown_hidden_and_manual_mapping_preserved_idempotently(db_session):
    db = db_session
    unknown = category(db, "自定义待核验", "unknown")
    hidden = category(db, "心理学", "hidden-psychology", visible=False)
    item = book(db, "未识别书", [unknown])
    db.add(CategoryMapping(source_main="编程开发", source_sub="", target_id=unknown.id))
    roots = upgrade(db)
    assert unknown.parent_id is None and unknown.is_visible and not hidden.is_visible
    assert [c.id for c in item.categories] == [unknown.id] and item.publish_status == "published"
    assert db.scalar(select(CategoryMapping).where(CategoryMapping.source_main == "编程开发")).target_id == unknown.id
    report = copy.deepcopy(db.get(SiteSetting, "catalog_upgrade_v1").value)
    assert unknown.id in report["unmapped_category_ids"] and item.id in report["review_resource_ids"]
    db.get(Category, roots["science"]).name = "技术阅读"
    db.commit()
    count = db.scalar(select(func.count(Category.id)))
    upgrade(db)
    assert db.scalar(select(func.count(Category.id))) == count
    assert db.get(Category, roots["science"]).name == "技术阅读"
    assert db.get(SiteSetting, "catalog_upgrade_v1").value == report


def test_duplicate_secondaries_merge_keep_urls_and_multi_branch_not_guessed(db_session, client):
    db = db_session
    one = db.get(Category, 1)
    two = category(db, "计算机互联网", "computer")
    a = category(db, "人工智能", "ai-one", one)
    b = category(db, "人工智能", "ai-two", two)
    mixed = category(db, "经济", "economy")
    r = book(db, "双重来源书", [two, b, mixed])
    roots = upgrade(db)
    assert db.get(CategoryRedirect, b.id).target_id == a.id
    assert client.get("/category/ai-two", follow_redirects=False).status_code == 301
    assert {roots["science"], roots["business"]}.issubset({c.id for c in r.categories})
    assert r.id in db.get(SiteSetting, "catalog_upgrade_v1").value["review_resource_ids"]


def test_new_upload_preview_does_not_create_then_commit_creates_only_secondary(client, db_session):
    db = db_session
    roots = upgrade(db)
    headers, data = auth(db), package()
    data["books"][0].update(main_category="计算机互联网", subcategory="人工智能")
    count = db.scalar(select(func.count(Category.id)))
    preview = client.post("/api/v1/organizer/preview", json=data, headers=headers)
    assert preview.status_code == 200
    planned = preview.json()["rows"][0]["mapped_categories"]
    assert planned[0]["id"] == roots["science"] and planned[-1]["id"] is None
    assert db.scalar(select(func.count(Category.id))) == count
    result = sync(client, headers, data)
    assert result["status"] == "ok", result
    uploaded = db.get(Resource, result["resource_id"])
    assert {c.name for c in uploaded.categories} == {"科学技术", "人工智能"}
    assert db.scalar(select(func.count(Category.id))) == count + 1
    assert [c.name for c in navigation_categories(db)] == [g[1] for g in GROUPS]
    later = sync(client, headers, next_revision(data), overwrite=True)
    assert later["status"] == "ok" and db.scalar(select(func.count(Category.id))) == count + 1
    path = resolve_categories(db, "科学技术", "人工智能")
    root_url = "/category/" + path[0].slug
    assert "人工智能" not in BeautifulSoup(client.get(root_url).text, "html.parser").select_one("main").get_text()
    uploaded.publish_status = "published"; db.commit()
    page = BeautifulSoup(client.get(root_url).text, "html.parser")
    assert "人工智能" in page.select_one(".sub-category-nav").get_text()
    assert page.select_one(".sub-category-nav").get_text().startswith("全部")


def test_manual_mapping_beats_auto_and_unknown_not_created(client, db_session):
    db = db_session
    roots = upgrade(db)
    save_mapping(db, "自定义书架", "专题", roots["history"]); db.commit()
    assert [c.id for c in resolve_categories(db, "自定义书架", "专题", allow_planned=True)] == [roots["history"]]
    data = package(); data["books"][0].update(main_category="无法识别的目录", subcategory="未知")
    count = db.scalar(select(func.count(Category.id)))
    result = sync(client, auth(db), data, publish=True)
    assert result["status"] == "ok" and result["publish_status"] == "draft"
    assert db.scalar(select(func.count(Category.id))) == count


@pytest.mark.parametrize("leaf", ["未分类", "公众号领取", "https://invalid.test", "未知"])
def test_bad_secondary_is_not_created(db_session, leaf):
    upgrade(db_session)
    with pytest.raises(ValueError):
        resolve_categories(db_session, "科学技术", leaf, allow_planned=True)


def test_materialize_reuses_child_created_since_preview(db_session):
    db = db_session
    roots = upgrade(db)
    pending = resolve_categories(db, "科学技术", "人工智能", allow_planned=True)
    made = category(db, "人工智能", "someone-created", db.get(Category, roots["science"]))
    db.commit()
    assert materialize(db, pending)[-1].id == made.id


def test_category_changed_after_preview_is_rejected(db_session):
    db = db_session
    roots = upgrade(db)
    pending = resolve_categories(db, "科学技术", "人工智能", allow_planned=True)
    root = db.get(Category, roots["science"])
    expected = plan_data(pending)
    root.is_visible = False
    db.commit()
    with pytest.raises(ValueError, match="预检后已变化"):
        materialize(db, pending)
    root.is_visible = True
    made = category(db, "人工智能", "changed-parent", root)
    committed = [root, made]
    assert same_plan(committed, expected)
    made.parent_id = roots["art"]
    assert not same_plan(committed, expected)


def test_category_page_aggregates_children_once_even_if_parent_association_missing(client, db_session):
    db = db_session
    roots = upgrade(db)
    root = db.get(Category, roots["science"])
    child = category(db, "人工智能", "ai", root)
    r = book(db, "缺父关联合成书", [root, child]); db.commit()
    r.categories = [child]; db.commit()
    html = client.get("/category/" + root.slug).text
    assert "缺父关联合成书" in html
    assert next(c.count for c in site_stats(db).category_counts if c.name == root.name) == 1


def test_fixed_roots_protected_and_manual_reparent_repairs_book(admin_client, db_session):
    db = db_session
    roots = upgrade(db)
    root = db.get(Category, roots["science"])
    child = category(db, "测试子类", "move-me", root)
    r = book(db, "人工移动合成书", [root, child]); db.commit()
    csrf = BeautifulSoup(admin_client.get("/admin/categories").text, "html.parser").select_one('[name="csrf_token"]')["value"]
    response = admin_client.post(f"/admin/categories/{child.id}/edit", data={"csrf_token": csrf, "name": child.name, "slug": child.slug, "parent_id": roots["art"], "is_visible": "1"})
    assert response.status_code == 200
    db.refresh(r)
    assert {c.id for c in r.categories} == {roots["art"], child.id}
    response = admin_client.post("/admin/categories", data={"csrf_token": csrf, "name": "不应进入导航", "is_visible": "1"})
    assert "顶部导航已固定" in response.text
    assert db.scalar(select(Category.id).where(Category.name == "不应进入导航")) is None
