from __future__ import annotations

import io
import json
import time

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import hash_password, verify_password
from app.main import create_app
from app.models import AdminOperationLog, AdminUser, SiteSetting
from app.services.site_settings import cover_hosts, put_value


def csrf(client):
    return BeautifulSoup(client.get("/admin/settings").text, "html.parser").select_one('[name="csrf_token"]')["value"]


def submit(client, section, **data):
    return client.post("/admin/settings/" + section, data={"csrf_token": csrf(client), **data})


def login(client, username="admin", password="Testing123!"):
    page = client.get("/admin/login")
    token = BeautifulSoup(page.text, "html.parser").select_one('[name="csrf_token"]')["value"]
    return client.post("/admin/login", data={"csrf_token": token, "username": username, "password": password}, follow_redirects=False)


@pytest.mark.parametrize("tab", ["site", "account", "domains", "sync", "updates"])
def test_settings_tabs_and_navigation(admin_client, tab):
    response = admin_client.get("/admin/settings?tab=" + tab)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "阶段一" not in response.text
    nav = BeautifulSoup(response.text, "html.parser").select_one(".admin-nav").get_text()
    assert "系统设置" in nav and "桌面同步" in nav and "云盘上传" not in nav


def test_auth_and_csrf_required(client, admin_client):
    assert admin_client.post("/admin/settings/site", data={"name": "unauthorized"}).status_code == 403
    admin_client.cookies.clear()
    assert client.get("/admin/settings", follow_redirects=False).status_code == 303
    assert client.post("/admin/settings/site", data={"name": "unauthorized"}, follow_redirects=False).status_code == 303


def test_profile_immediate_and_escaped(admin_client, db_session):
    response = submit(admin_client, "site", name="新书库<script>alert(1)</script>", description="新介绍", footer="页脚说明", contact_email="owner@example.com")
    assert response.status_code == 200
    home = admin_client.get("/").text
    assert "新介绍" in home and "页脚说明" in home and "owner@example.com" in home
    assert "&lt;script&gt;" in home and "<script>alert(1)</script>" not in home
    assert db_session.get(SiteSetting, "profile").value["name"].startswith("新书库")
    admin_client.cookies.clear()
    assert "新书库" in admin_client.get("/admin/login").text


def test_profile_invalid_not_applied(admin_client, db_session):
    response = submit(admin_client, "site", name="", contact_email="wrong")
    assert "请填写网站名称" in response.text
    assert db_session.get(SiteSetting, "profile") is None


def test_home_hero_editable_newlines_escaped_and_paragraph_removed(admin_client, db_session):
    old = BeautifulSoup(admin_client.get("/").text, "html.parser")
    assert "找到值得读的那一本" in old.select_one(".hero h1").get_text()
    assert old.select_one(".hero-lead") is None and old.select_one(".hero-stats") is not None
    submit(admin_client, "site", name="合成书库", hero_eyebrow="每天读一点", hero_title="第一行\r\n<script>不能执行</script>")
    html = admin_client.get("/").text
    page = BeautifulSoup(html, "html.parser")
    assert page.select_one(".hero h1").get_text() == "第一行\n<script>不能执行</script>"
    assert "<script>不能执行</script>" not in html
    assert page.select_one(".hero .eyebrow").get_text() == "每天读一点"
    submit(admin_client, "site", name="只改站名")
    assert "第一行" in admin_client.get("/").text
    submit(admin_client, "site", name="合成书库", hero_eyebrow="", hero_title="")
    page = BeautifulSoup(admin_client.get("/").text, "html.parser")
    assert page.select_one(".hero h1") is None and page.select_one(".hero .eyebrow") is None
    assert page.select_one(".hero-stats") is not None


@pytest.mark.parametrize("value", ["字" * 181, "一\n二\n三\n四\n五", "不可\x00用"])
def test_hero_invalid_content_is_atomic(admin_client, db_session, value):
    response = submit(admin_client, "site", name="不能保存", hero_title=value)
    assert "首页标语" in response.text
    assert db_session.get(SiteSetting, "profile") is None


def test_logo_upload_reencoded_and_served(admin_client, monkeypatch, tmp_path, db_session):
    monkeypatch.setattr(get_settings(), "local_storage_root", tmp_path)
    source = io.BytesIO()
    Image.new("RGB", (1024, 768), "green").save(source, "JPEG")
    response = admin_client.post("/admin/settings/site", data={"csrf_token": csrf(admin_client), "name": "品牌测试"},
        files={"logo": ("../../logo.html", source.getvalue(), "text/html")})
    assert response.status_code == 200
    url = db_session.get(SiteSetting, "profile").value["logo"]
    image = admin_client.get(url)
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(image.content)) as output:
        assert max(output.size) == 512
    assert url in admin_client.get("/").text
    submit(admin_client, "site", name="品牌测试", remove_logo="yes")
    assert db_session.get(SiteSetting, "profile").value["logo"] == ""
    # 不会删除仍可能被缓存或旧页面引用的图片。
    assert admin_client.get(url).status_code == 200


@pytest.mark.parametrize("payload", [b'<svg onload="alert(1)"></svg>', b'x' * (2 * 1024 * 1024 + 1)], ids=["svg", "oversize"])
def test_logo_rejects_scripts_and_oversize(admin_client, payload, db_session):
    response = admin_client.post("/admin/settings/site", data={"csrf_token": csrf(admin_client), "name": "品牌测试"},
        files={"logo": ("logo.svg", payload, "image/svg+xml")})
    assert db_session.get(SiteSetting, "profile") is None
    assert "图片" in response.text


def test_account_changes_invalidate_other_sessions(admin_client, db_session):
    second_app = create_app()
    second_app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(second_app) as other:
        assert login(other).status_code == 303
        response = submit(admin_client, "account", username="newowner", display_name="站长", current_password="Testing123!",
            new_password="MyNewPassword123!", confirm_password="MyNewPassword123!")
        assert "/admin/login" in str(response.url)
        assert other.get("/admin", follow_redirects=False).status_code == 303
        assert login(other, "newowner", "MyNewPassword123!").status_code == 303
        user = db_session.scalar(select(AdminUser))
        assert user.username == "newowner" and verify_password("MyNewPassword123!", user.password_hash)
    logs = list(db_session.scalars(select(AdminOperationLog)))
    assert all("MyNewPassword" not in json.dumps(row.detail) for row in logs)


def test_cli_style_password_reset_invalidates_existing_login(admin_client, db_session):
    user = db_session.scalar(select(AdminUser))
    user.password_hash = hash_password("ResetPassword123!")
    db_session.commit()
    assert admin_client.get("/admin/settings", follow_redirects=False).status_code == 303


def test_cli_reset_targets_renamed_admin(db_session, monkeypatch):
    from contextlib import contextmanager
    from scripts import create_admin
    user = db_session.scalar(select(AdminUser))
    user.username = "renamedowner"
    db_session.commit()
    @contextmanager
    def session():
        yield db_session
    monkeypatch.setattr(create_admin, "SessionLocal", session)
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda _: "EmergencyPassword123!")
    monkeypatch.setattr("sys.argv", ["create_admin"])
    create_admin.main()
    assert db_session.scalar(select(AdminUser).where(AdminUser.username == "admin")) is None
    assert verify_password("EmergencyPassword123!", user.password_hash)


def test_cover_whitelist_change_blocks_old_preview_commit(client, db_session):
    import hashlib
    import uuid
    from app.models import OrganizerToken, Resource
    secret = "isolated-test-sync-token"
    db_session.add(OrganizerToken(admin_user_id=1, label="测试", token_hash=hashlib.sha256(secret.encode()).hexdigest(), is_active=True))
    put_value(db_session, "sync", {"cover_hosts": ["images.example.com"]})
    db_session.commit()
    export_id, book_id = uuid.uuid4().hex, "BK_" + uuid.uuid4().hex
    payload = {"schema_version": "2.0", "site_id": "jingye-local", "workspace_id": "test", "export_id": export_id,
        "books": [{"book_id": book_id, "revision": 1, "epub_sha256": "a"*64, "title": "合成图书", "cover_url": "https://images.example.com/a.png"}]}
    headers = {"Authorization": "Bearer " + secret}
    assert client.post("/api/v1/organizer/preview", json=payload, headers=headers).status_code == 200
    put_value(db_session, "sync", {"cover_hosts": []})
    db_session.commit()
    response = client.post(f"/api/v1/organizer/batches/{export_id}/commit", json={"choices": [{"book_id": book_id, "action": "create"}]}, headers=headers)
    assert response.status_code == 409
    assert db_session.scalar(select(Resource)) is None


def test_wrong_password_cannot_change_account(admin_client, db_session):
    response = submit(admin_client, "account", username="other", display_name="管理员", current_password="wrong")
    assert "当前密码不正确" in response.text
    assert db_session.scalar(select(AdminUser)).username == "admin"


def test_sync_hosts_override_and_explicit_clear(admin_client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "organizer_cover_hosts", "old.example.com")
    submit(admin_client, "sync", cover_hosts="IMG.example.com\nsecond.example.org")
    assert cover_hosts(db_session) == ["img.example.com", "second.example.org"]
    submit(admin_client, "sync", cover_hosts="")
    assert cover_hosts(db_session) == []
    assert "尚未配置" in admin_client.get("/admin/organizer").text


def test_domain_draft_does_not_change_runtime(admin_client, db_session):
    original = get_settings().public_base_url
    response = submit(admin_client, "domains", primary="new.example.com", aliases="www.example.com", apply="no")
    assert "尚未更改实际访问地址" in response.text
    assert get_settings().public_base_url == original
    assert db_session.get(SiteSetting, "domain_draft").value["primary"] == "new.example.com"


def test_no_agent_cannot_fake_success(admin_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "maintenance_control_root", None)
    response = submit(admin_client, "domains", primary="new.example.com", aliases="", apply="yes", confirm="yes", current_password="Testing123!")
    assert "尚未连接服务器维护服务" in response.text


def test_release_check_manual_and_update_pinned(admin_client, db_session, monkeypatch, tmp_path):
    import app.admin.settings as settings_routes
    info = {"tag": "v2.0.0", "sha": "a" * 40, "notes": "<script>alert(1)</script>", "url": "https://github.com/onlylm/shuku/releases/tag/v2.0.0", "available": True, "checked_at": time.time()}
    called = []
    monkeypatch.setattr(settings_routes, "release_info", lambda: called.append(True) or info)
    assert not called
    response = submit(admin_client, "check-update")
    assert len(called) == 1 and "&lt;script&gt;" in response.text
    root = tmp_path / "control"
    (root / "requests").mkdir(parents=True)
    (root / "status").mkdir()
    (root / "status/heartbeat.json").write_text(json.dumps({"protocol": 1, "time": time.time()}))
    monkeypatch.setattr(get_settings(), "maintenance_control_root", root)
    bad = submit(admin_client, "update", tag="v9.0.0", sha="a"*40, current_password="Testing123!", confirm="yes")
    assert "版本信息已变化" in bad.text and not (root / "requests/pending.json").exists()
    good = submit(admin_client, "update", tag=info["tag"], sha=info["sha"], current_password="Testing123!", confirm="yes")
    assert "维护任务已提交" in good.text
    job = json.loads((root / "requests/pending.json").read_text())
    assert job["payload"] == {"tag": "v2.0.0", "sha": "a" * 40}
    assert "password" not in json.dumps(job)
    again = submit(admin_client, "backup", current_password="Testing123!", confirm="yes")
    assert "已有维护任务" in again.text


def test_production_upload_endpoints_disabled(db_session, monkeypatch):
    from app.core.config import Settings
    import app.main as main
    config = Settings(_env_file=None, app_env="production", debug=False, session_https_only=True, session_secret="x"*48,
        public_base_url="https://books.example.com", cloud_upload_worker_enabled=False)
    monkeypatch.setattr(main, "get_settings", lambda: config)
    app = main.create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, base_url="https://books.example.com") as client:
        for path in ("/admin/uploads", "/admin/uploads/scan", "/admin/uploads/quark/install"):
            assert client.get(path).status_code == 404
            assert client.post(path).status_code == 404
