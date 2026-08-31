import hashlib
import uuid

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.core.config import get_settings
from app.models import OrganizerToken, Resource, AdminUser, OrganizerIdentity
from app.services.links import add_or_replace_link, visible_redirect_link
from app.services.resources import create_resource


def auth(db):
    secret = "testing-organizer-token"
    token = OrganizerToken(admin_user_id=1, label="测试", token_hash=hashlib.sha256(secret.encode()).hexdigest(), is_active=True)
    db.add(token); db.commit()
    return {"Authorization": "Bearer " + secret}


def package():
    return {"schema_version": "2.0", "site_id": "jingye-local", "workspace_id": "test", "export_id": uuid.uuid4().hex, "books": [{"book_id": "BK_" + uuid.uuid4().hex, "revision": 1, "epub_sha256": "a" * 64, "title": "测试版本", "author": "作者", "rights_review_status": "confirmed", "copyright_status": "public_domain", "source_reference": "合成测试资料", "main_category": "文学", "subcategory": "小说", "formats": "EPUB"}]}


def test_auth_and_revoke(client, db_session):
    assert client.get("/api/v1/organizer/info").status_code == 401
    headers = auth(db_session)
    assert client.get("/api/v1/organizer/info", headers=headers).status_code == 200
    token = db_session.scalar(select(OrganizerToken)); token.is_active = False; db_session.commit()
    assert client.get("/api/v1/organizer/info", headers=headers).status_code == 401


def test_preview_commit_idempotency_and_draft(client, db_session):
    headers, data = auth(db_session), package()
    response = client.post("/api/v1/organizer/preview", json=data, headers=headers)
    assert response.status_code == 200, response.text
    assert db_session.scalar(select(Resource)) is None
    choices = {"choices": [{"book_id": data["books"][0]["book_id"], "action": "create"}]}
    url = "/api/v1/organizer/batches/" + data["export_id"] + "/commit"
    result = client.post(url, json=choices, headers=headers)
    assert result.status_code == 200, result.text
    row = next(iter(result.json()["items"].values()))
    assert row["status"] == "ok", row
    assert row["publish_status"] == "draft"
    again = client.post(url, json=choices, headers=headers)
    assert again.json() == result.json()
    assert len(list(db_session.scalars(select(Resource)))) == 1
    assert db_session.scalar(select(OrganizerIdentity)).epub_sha256 == "a" * 64


def test_rights_pending_blocked(client, db_session):
    headers, data = auth(db_session), package()
    data["books"][0]["rights_review_status"] = "pending"
    response = client.post("/api/v1/organizer/preview", json=data, headers=headers)
    assert response.json()["rows"][0]["error"]
    result = client.post(f"/api/v1/organizer/batches/{data['export_id']}/commit", json={"choices": [{"book_id": data["books"][0]["book_id"], "action": "create"}]}, headers=headers)
    assert next(iter(result.json()["items"].values()))["status"] == "error"
    assert db_session.scalar(select(Resource)) is None


def test_site_and_cover_validation(client, db_session):
    headers, data = auth(db_session), package()
    data["site_id"] = "another-site"
    assert client.post("/api/v1/organizer/preview", json=data, headers=headers).status_code == 409
    data["site_id"] = "jingye-local"; data["books"][0]["cover_url"] = "http://127.0.0.1/private"
    assert client.post("/api/v1/organizer/preview", json=data, headers=headers).status_code == 409
    data["books"][0]["local_path"] = "D:/private/book.epub"
    assert client.post("/api/v1/organizer/preview", json=data, headers=headers).status_code == 422


def test_stale_preview_does_not_overwrite(client, db_session):
    resource = create_resource(db_session, {"title": "测试版本", "author": "旧作者"}); db_session.commit()
    headers, data = auth(db_session), package()
    response = client.post("/api/v1/organizer/preview", json=data, headers=headers)
    assert response.json()["rows"][0]["action"] == "choose"
    resource.author = "人工刚修改"; db_session.commit()
    result = client.post(f"/api/v1/organizer/batches/{data['export_id']}/commit", json={"choices": [{"book_id": data["books"][0]["book_id"], "action": "bind", "resource_id": resource.id, "overwrite": True}]}, headers=headers)
    assert next(iter(result.json()["items"].values()))["status"] == "error"
    assert resource.author == "人工刚修改"


def test_unpublished_resource_old_redirect_is_hidden(db_session):
    resource = create_resource(db_session, {"title": "下架测试", "publish_status": "draft"})
    link = add_or_replace_link(db_session, resource.id, "https://pan.quark.cn/s/testhidden")
    link.status = "active"; link.is_visible = True; db_session.commit()
    assert visible_redirect_link(db_session, link.id) is None
    resource.publish_status = "published"; db_session.commit()
    assert visible_redirect_link(db_session, link.id) is not None


def test_admin_token_page_and_disabled_session(admin_client, db_session):
    page = admin_client.get("/admin/organizer")
    assert page.status_code == 200
    csrf = BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf_token"]')["value"]
    response = admin_client.post("/admin/organizer/tokens", data={"csrf_token": csrf, "label": "测试电脑"})
    assert response.status_code == 200 and "eo_" in response.text
    assert response.headers["cache-control"] == "no-store"
    admin = db_session.get(AdminUser, 1); admin.is_active = False; db_session.commit()
    assert admin_client.get("/admin/organizer", follow_redirects=False).status_code == 303


def test_binding_preserves_existing_slug_and_nonempty_fields(client, db_session):
    resource = create_resource(db_session, {"title": "测试版本", "author": "人工维护作者"})
    original_slug, original_code = resource.slug, resource.resource_code
    db_session.commit()
    headers, data = auth(db_session), package()
    data["books"][0]["publisher"] = "待补出版社"
    client.post("/api/v1/organizer/preview", json=data, headers=headers)
    result = client.post(f"/api/v1/organizer/batches/{data['export_id']}/commit", json={"choices": [{"book_id": data["books"][0]["book_id"], "action": "bind", "resource_id": resource.id}]}, headers=headers)
    assert next(iter(result.json()["items"].values()))["status"] == "ok"
    db_session.refresh(resource)
    assert resource.author == "人工维护作者" and resource.publisher == "待补出版社"
    assert (resource.slug, resource.resource_code) == (original_slug, original_code)


def test_duplicate_link_rollback_does_not_leave_new_resource(client, db_session):
    resource = create_resource(db_session, {"title": "另一部作品"})
    link = add_or_replace_link(db_session, resource.id, "https://pan.quark.cn/s/existingunique")
    db_session.commit()
    headers, data = auth(db_session), package()
    data["books"][0]["links"] = [{"url": link.normalized_url}]
    client.post("/api/v1/organizer/preview", json=data, headers=headers)
    result = client.post(f"/api/v1/organizer/batches/{data['export_id']}/commit", json={"choices": [{"book_id": data["books"][0]["book_id"], "action": "create"}]}, headers=headers)
    assert next(iter(result.json()["items"].values()))["status"] == "error"
    assert len(list(db_session.scalars(select(Resource)))) == 1


def test_publish_requires_fresh_success_not_old_active(client, db_session, monkeypatch):
    from types import SimpleNamespace
    from app.services import organizer_sync
    resource = create_resource(db_session, {"title": "测试版本", "publish_status": "draft"})
    link = add_or_replace_link(db_session, resource.id, "https://pan.quark.cn/s/freshcheck")
    link.status = "active"; link.is_visible = True; db_session.commit()
    monkeypatch.setattr(organizer_sync, "check_link", lambda db, link: SimpleNamespace(result="error"))
    headers, data = auth(db_session), package()
    data["books"][0]["links"] = [{"url": link.normalized_url}]
    client.post("/api/v1/organizer/preview", json=data, headers=headers)
    result = client.post(f"/api/v1/organizer/batches/{data['export_id']}/commit", json={"choices": [{"book_id": data["books"][0]["book_id"], "action": "bind", "resource_id": resource.id, "publish": True}]}, headers=headers)
    row = next(iter(result.json()["items"].values()))
    assert row["status"] == "ok" and row["publish_status"] == "draft"


def test_token_creation_requires_csrf(admin_client):
    assert admin_client.post("/admin/organizer/tokens", data={"label": "无防伪令牌"}).status_code == 403
