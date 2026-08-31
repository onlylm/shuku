import copy
import uuid
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.models import AdminOperationLog, Category, CategoryMapping, CategoryRedirect, OrganizerIdentity, Resource
from app.services.category_governance import catalog_audit, merge_categories, merge_preview, resolve_categories, save_mapping
from app.services.metadata_import import commit_meta_preview, create_meta_preview
from app.services.organizer_sync import fingerprint
from app.services.publication import publication_issues
from app.services.resources import create_resource, update_resource
from tests.test_organizer_sync import auth, package


def valid_book(db, **changes):
    return create_resource(db, {"title": "合成书目", "copyright_status": "authorized", "source_reference": "合成测试资料",
                               "category_ids": [1], "publish_status": "published", **changes})


def sync(client, headers, data, **choice):
    preview = client.post("/api/v1/organizer/preview", json=data, headers=headers)
    assert preview.status_code == 200, preview.text
    row = preview.json()["rows"][0]
    result = client.post(f"/api/v1/organizer/batches/{data['export_id']}/commit", headers=headers,
                        json={"choices": [{"book_id": data["books"][0]["book_id"], "action": row["action"], **choice}]})
    assert result.status_code == 200, result.text
    return next(iter(result.json()["items"].values()))


def mapped_package(db):
    data = package()
    save_mapping(db, "文学", "小说", 1)
    db.commit()
    return data


def next_revision(data, **changes):
    data = copy.deepcopy(data)
    data["export_id"] = uuid.uuid4().hex
    data["books"][0]["revision"] += 1
    data["books"][0].update(changes)
    return data


def test_defaults_gate_and_partial_edit_keep_url_and_facts(db_session):
    draft = create_resource(db_session, {"title": "待核验图书", "publish_status": "published"})
    assert draft.copyright_status == "pending" and draft.publish_status == "draft"
    book = valid_book(db_session, isbn="9787020065512", publish_year=2017)
    old_slug = book.slug
    update_resource(db_session, book, {"title": "精简后的书名", "slug": "不应改变旧链接"})
    assert book.publish_status == "published"
    assert book.slug == old_slug and book.isbn == "9787020065512" and book.publish_year == 2017


@pytest.mark.parametrize("field,value", [("author", "公众号：资料领取"), ("publisher", "https://spam.invalid"), ("publisher", "扫码免费领取"), ("copyright_status", "unknown"), ("source_reference", "")])
def test_bad_metadata_never_auto_publishes(db_session, field, value):
    book = valid_book(db_session, **{field: value})
    assert book.publish_status == "draft"
    assert publication_issues(book)


def test_linkless_book_remains_in_detail_search_list_sitemap(client, db_session):
    book = valid_book(db_session, title="无入口测试书")
    db_session.commit()
    original_fingerprint = fingerprint(book)
    original_updated_at = book.updated_at
    assert client.get(f"/book/id/{book.id}").status_code == 200
    assert "网盘入口暂不可用" in client.get(f"/book/id/{book.id}").text
    assert "无入口测试书" in client.get("/books").text
    assert "无入口测试书" in client.get("/search?q=无入口测试书").text
    assert f"/book/id/{book.id}" in client.get("/sitemap.xml").text
    db_session.refresh(book)
    assert fingerprint(book) == original_fingerprint
    assert book.updated_at.replace(tzinfo=None) == original_updated_at.replace(tzinfo=None)
    book.publish_status = "archived"; db_session.commit()
    assert client.get(f"/book/id/{book.id}").status_code == 404
    assert f"/book/id/{book.id}" not in client.get("/sitemap.xml").text


def test_unknown_category_saves_draft_and_does_not_create_categories(client, db_session, monkeypatch):
    from app.services import organizer_sync
    monkeypatch.setattr(organizer_sync, "check_link", lambda *a: pytest.fail("不合格资料不应触发发布检测"))
    headers, data = auth(db_session), package()
    data["books"][0]["links"] = [{"url": "https://pan.quark.cn/s/synthetic"}]
    result = sync(client, headers, data, publish=True)
    assert result["status"] == "ok" and result["publish_status"] == "draft"
    assert "尚未映射" in "".join(result["publication_issues"])
    assert db_session.scalar(select(func.count(Category.id))) == 1
    audit = catalog_audit(db_session)
    assert len(audit["pending_categories"]) == 1


def test_exact_mapping_does_not_swallow_unmapped_subcategory(db_session):
    save_mapping(db_session, "电脑技术", "", 1); db_session.commit()
    assert [c.id for c in resolve_categories(db_session, "电脑技术")] == [1]
    with pytest.raises(ValueError):
        resolve_categories(db_session, "电脑技术", "不明二级")
    assert db_session.scalar(select(func.count(Category.id))) == 1


def test_sync_replaces_categories_by_identity_and_preserves_slug(client, db_session):
    data, headers = mapped_package(db_session), auth(db_session)
    first = sync(client, headers, data)
    assert first["status"] == "ok", first
    book = db_session.get(Resource, first["resource_id"])
    old_slug = book.slug
    root = Category(name="历史人文", slug="history-humanities")
    db_session.add(root); db_session.flush()
    child = Category(name="中国史", slug="chinese-history", parent_id=root.id)
    db_session.add(child); db_session.commit()
    updated = next_revision(data, title="更短的书名", main_category="历史人文", subcategory="中国史")
    result = sync(client, headers, updated, overwrite=True)
    assert result["status"] == "ok", result
    db_session.refresh(book)
    assert {c.id for c in book.categories} == {root.id, child.id}
    assert book.title == "更短的书名" and book.slug == old_slug
    assert result["resource_id"] == first["resource_id"]
    assert db_session.scalar(select(func.count(Resource.id))) == 1


def test_same_revision_mutation_blocked(client, db_session):
    data, headers = mapped_package(db_session), auth(db_session)
    first = sync(client, headers, data)
    changed = copy.deepcopy(data); changed["export_id"] = uuid.uuid4().hex
    changed["books"][0]["author"] = "伪旧修订"
    result = sync(client, headers, changed, overwrite=True)
    assert result["status"] == "error"
    assert db_session.get(Resource, first["resource_id"]).author == "作者"


def test_reviewed_metadata_and_categories_survive_desktop_overwrite(client, db_session):
    data, headers = mapped_package(db_session), auth(db_session)
    first = sync(client, headers, data)
    book = db_session.get(Resource, first["resource_id"])
    update_resource(db_session, book, {"title": "网站审核书名", "author": "审核作者", "publisher": "审核出版社",
                                    "description": "人工审核简介", "metadata_locked": True})
    db_session.commit()
    result = sync(client, headers, next_revision(data, title="旧书名", author="旧作者", publisher="旧出版社", description="旧简介", main_category="不明来源", subcategory="", links=[{"url": "https://pan.quark.cn/s/locked-book"}]), overwrite=True)
    assert result["status"] == "ok", result
    db_session.refresh(book)
    assert (book.title, book.author, book.publisher, book.description) == ("网站审核书名", "审核作者", "审核出版社", "人工审核简介")
    assert [c.id for c in book.categories] == [1]
    assert len(result["links"]) == 1


def test_valid_sync_can_publish_with_fresh_link_check(client, db_session, monkeypatch):
    from app.services import organizer_sync
    def good(db, link):
        link.status = "active"; link.is_visible = True
        return SimpleNamespace(result="ok")
    monkeypatch.setattr(organizer_sync, "check_link", good)
    data, headers = mapped_package(db_session), auth(db_session)
    data["books"][0]["links"] = [{"url": "https://pan.quark.cn/s/publish-good"}]
    result = sync(client, headers, data, publish=True)
    assert result["status"] == "ok" and result["publish_status"] == "published", result


def test_merging_is_previewed_keeps_records_and_old_urls(client, db_session):
    source = Category(name="编程", slug="old-programming")
    db_session.add(source); db_session.flush()
    book = valid_book(db_session, category_ids=[source.id])
    db_session.commit()
    preview = merge_preview(db_session, source.id, 1)
    assert source.is_visible and [c.id for c in book.categories] == [source.id]
    assert db_session.scalar(select(AdminOperationLog)) is None
    # 与真实HTTP请求相同，释放ORM缓存后再提交。
    db_session.expire_all()
    log = merge_categories(db_session, source.id, 1, preview["fingerprint"], 1)
    db_session.commit()
    assert db_session.get(Category, source.id) is not None
    assert not source.is_visible and [c.id for c in book.categories] == [1]
    assert book.metadata_locked
    assert log.detail["rows"][0]["before"] == [source.id]
    response = client.get("/category/old-programming?page=2", follow_redirects=False)
    assert response.status_code == 301 and response.headers["location"].endswith("/category/programming?page=2")
    assert "/category/old-programming" not in client.get("/sitemap.xml").text
    with pytest.raises(ValueError):
        merge_categories(db_session, source.id, 1, preview["fingerprint"], 1)


def test_stale_merge_preview_refuses_changes(db_session):
    source = Category(name="旧编程", slug="old-code")
    db_session.add(source); db_session.flush()
    book = valid_book(db_session, category_ids=[source.id]); db_session.commit()
    preview = merge_preview(db_session, source.id, 1)
    book.title = "人工修改"; db_session.commit()
    with pytest.raises(ValueError, match="重新预览"):
        merge_categories(db_session, source.id, 1, preview["fingerprint"], 1)
    assert [c.id for c in book.categories] == [source.id]
    assert db_session.get(CategoryRedirect, source.id) is None


def test_admin_mapping_and_preview_pages_protected_and_render(client, admin_client, db_session):
    page = admin_client.get("/admin/categories/governance")
    assert page.status_code == 200 and "待映射图书" in page.text
    csrf = BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf_token"]')["value"]
    denied = admin_client.post("/admin/categories/mapping", data={"source_main": "编程", "target_id": 1})
    assert denied.status_code == 403
    ok = admin_client.post("/admin/categories/mapping", data={"csrf_token": csrf, "source_main": "编程", "target_id": 1})
    assert ok.status_code == 200 and "已有映射（1）" in ok.text
    source = Category(name="旧类", slug="legacy")
    db_session.add(source); db_session.commit()
    preview = admin_client.post("/admin/categories/merge-preview", data={"csrf_token": csrf, "source_id": source.id, "target_id": 1})
    assert preview.status_code == 200 and "取消，不修改" in preview.text
    assert db_session.get(CategoryRedirect, source.id) is None


def test_metadata_table_resolves_system_id_without_title_fallback(db_session):
    book = create_resource(db_session, {"title": "旧书名"})
    bid = "BK_" + uuid.uuid4().hex
    db_session.add(OrganizerIdentity(book_id=bid, resource_id=book.id, epub_sha256="a"*64, revision=1, payload_hash="x"*64)); db_session.commit()
    content = f"系统编号,书名,出版社\n{bid},全新书名,正确出版社\nBK_{'0'*32},旧书名,不应错填\n".encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", content, 1)
    assert [r.row_status for r in batch.rows] == ["ready", "unmatched"]
    commit_meta_preview(db_session, batch, {r.id for r in batch.rows})
    assert book.publisher == "正确出版社" and book.metadata_locked


def test_metadata_preview_cannot_overwrite_later_manual_edit(db_session):
    book = create_resource(db_session, {"title": "测试书"}); db_session.commit()
    batch = create_meta_preview(db_session, "meta.csv", "书名,作者\n测试书,表格作者\n".encode(), 1)
    book.author = "网站新作者"; db_session.commit()
    result = commit_meta_preview(db_session, batch, {batch.rows[0].id}, overwrite=True)
    assert result.skipped == 1 and book.author == "网站新作者"


def test_mapping_changed_after_sync_preview_requires_new_preview(client, db_session):
    data, headers = mapped_package(db_session), auth(db_session)
    response = client.post("/api/v1/organizer/preview", json=data, headers=headers)
    assert response.status_code == 200
    category = Category(name="另一个分类", slug="another")
    db_session.add(category); db_session.flush()
    save_mapping(db_session, "文学", "小说", category.id); db_session.commit()
    result = client.post(f"/api/v1/organizer/batches/{data['export_id']}/commit", headers=headers,
                         json={"choices": [{"book_id": data["books"][0]["book_id"], "action": "create"}]})
    row = next(iter(result.json()["items"].values()))
    assert row["status"] == "error" and "映射已改变" in row["message"]
    assert db_session.scalar(select(func.count(Resource.id))) == 0


def test_governance_requires_login(client):
    assert client.get("/admin/categories/governance", follow_redirects=False).status_code == 303


def test_old_or_another_identity_cannot_overwrite_bound_book(client, db_session):
    data, headers = mapped_package(db_session), auth(db_session)
    first = sync(client, headers, data)
    newer = next_revision(data, title="新版书名")
    assert sync(client, headers, newer, overwrite=True)["status"] == "ok"
    old = copy.deepcopy(data); old["export_id"] = uuid.uuid4().hex
    assert sync(client, headers, old, overwrite=True)["status"] == "error"
    another = package(); another["books"][0]["title"] = "新版书名"
    result = sync(client, headers, another, action="bind", resource_id=first["resource_id"])
    assert result["status"] == "error" and "其他版本编号" in result["message"]
    assert db_session.get(Resource, first["resource_id"]).title == "新版书名"
