from __future__ import annotations

import io
from pathlib import Path
import tarfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, func
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.core.security import verify_password
from app.models import AdminUser, Category, Provider, Resource
from scripts.init_production import initialize
from scripts.server_config import create_config, hostname, make_config, read_config, validate
from scripts.server_backup import seal, validate_archive, verify


@pytest.mark.parametrize("value", ["", "localhost", "127.0.0.1", "https://books.example.com", "a.local", "x.test",
                                  "books.example.com/path", "books.example.com:80", "a.example.com\nother.com",
                                  "*.example.com", "a..com", "-a.example.com", "x.example.com}"])
def test_domain_input_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        hostname(value)


def test_generated_config_private_unique_and_not_overwritten(tmp_path):
    path = tmp_path / "deploy" / ".env"
    first = make_config("Books.Example.com", "我的书库", "admin")
    create_config(path, first)
    assert read_config(path) == first
    assert first["SITE_DOMAIN"] == "books.example.com"
    second = make_config("else.example.com", "另一个书库", "admin")
    assert first["SESSION_SECRET"] != second["SESSION_SECRET"]
    assert first["ORGANIZER_SITE_ID"] != second["ORGANIZER_SITE_ID"]
    with pytest.raises(FileExistsError):
        create_config(path, second)
    assert read_config(path) == first
    assert "lmonly" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("payload", ["APP_NAME=$(whoami)", "APP_NAME=`whoami`", "SITE_DOMAIN=a\nSITE_DOMAIN=b", "export APP_NAME=x"])
def test_config_never_executes_shell(tmp_path, payload):
    path = tmp_path / ".env"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        read_config(path)


def test_server_template_has_no_selected_domain_or_credentials():
    path = Path(__file__).parents[1] / "deploy" / ".env.example"
    values = read_config(path)
    assert values["SITE_DOMAIN"] == ""
    assert values["ORGANIZER_COVER_HOSTS"] == ""
    assert values["MYSQL_PASSWORD"] == ""
    with pytest.raises(ValueError):
        validate(values)


@pytest.mark.parametrize("url", ["http://books.example.com", "http://127.0.0.1:8000", "https://127.0.0.1",
                                "https://books.example.com/path", "https://a.local", "https://name:pw@books.example.com",
                                "https://books.example.com?wrong=1"])
def test_production_cannot_use_development_or_malformed_url(url):
    config = Settings(_env_file=None, app_env="production", debug=False, session_https_only=True,
                      session_secret="a" * 48, public_base_url=url)
    with pytest.raises(RuntimeError):
        config.validate_runtime_safety()


def test_production_host_and_api_documentation_guards(monkeypatch, db_session):
    import app.main as main
    from app.core.database import get_db

    config = Settings(_env_file=None, app_name="部署测试书库", app_env="production", debug=False, session_https_only=True,
                      session_secret="a" * 48, public_base_url="https://books.example.com",
                      cloud_upload_worker_enabled=False, link_check_automatic_enabled=False)
    config.validate_runtime_safety()
    monkeypatch.setattr(main, "get_settings", lambda: config)
    app = main.create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app, base_url="https://books.example.com") as client:
        assert client.get("/api/v1/ready").status_code == 200
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/v1/ready", headers={"host": "evil.example.net"}).status_code == 400
        assert client.get("/api/v1/ready", headers={"host": "127.0.0.1:8000"}).status_code == 200
        response = client.get("/admin/login")
        assert "secure" in response.headers["set-cookie"].lower()
        assert "部署测试书库" in response.text
        assert "部署测试书库" in client.get("/").text


def test_readiness_checks_database_and_hides_connection_error(client, db_session, monkeypatch):
    assert client.get("/api/v1/ready").json() == {"status": "ready"}

    def broken(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("mysql://private:password@db"))

    monkeypatch.setattr(db_session, "execute", broken)
    response = client.get("/api/v1/ready")
    assert response.status_code == 503
    assert "password" not in response.text


def test_production_initializer_never_resets_existing_admin_or_seeds_books(db_session):
    existing = db_session.scalar(select(AdminUser))
    password_hash = existing.password_hash
    assert initialize(db_session, "new_admin", "a" * 32) is False
    assert initialize(db_session, "new_admin", "b" * 32) is False
    assert existing.password_hash == password_hash
    assert db_session.scalar(select(func.count()).select_from(AdminUser)) == 1
    assert db_session.scalar(select(func.count()).select_from(Provider)) == 2
    assert db_session.scalar(select(func.count()).select_from(Resource)) == 0
    assert db_session.scalar(select(func.count()).select_from(Category)) == 1


def test_production_initializer_fresh_database(db_session):
    db_session.execute(delete(AdminUser))
    db_session.execute(delete(Category))
    db_session.commit()
    with pytest.raises(ValueError):
        initialize(db_session, "admin", "ChangeMe123!")
    assert initialize(db_session, "owner", "random-test-password-123") is True
    assert verify_password("random-test-password-123", db_session.scalar(select(AdminUser)).password_hash)
    assert db_session.scalar(select(func.count()).select_from(Category)) == 8
    assert db_session.scalar(select(func.count()).select_from(Resource)) == 0


def archive(path, name="runtime/test.txt", kind=tarfile.REGTYPE):
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
        info.size = 1 if kind == tarfile.REGTYPE else 0
        tar.addfile(info, io.BytesIO(b"a") if info.size else None)


def test_backup_seal_and_tampering_detection(tmp_path):
    archive(tmp_path / "files.tar.gz")
    (tmp_path / "database.sql.gz").write_bytes(b"fixture-db")
    (tmp_path / "deploy.env").write_text("fixture-config", encoding="utf-8")
    seal(tmp_path, "test-revision")
    verify(tmp_path)
    (tmp_path / "database.sql.gz").write_bytes(b"modified")
    with pytest.raises(ValueError, match="校验失败"):
        verify(tmp_path)


@pytest.mark.parametrize("name,kind", [("../etc/passwd", tarfile.REGTYPE), ("/app/runtime/x", tarfile.REGTYPE),
                                     ("app/main.py", tarfile.REGTYPE), ("runtime/link", tarfile.SYMTYPE),
                                     ("runtime/hardlink", tarfile.LNKTYPE), ("runtime/device", tarfile.CHRTYPE)])
def test_backup_rejects_escape_and_special_files(tmp_path, name, kind):
    path = tmp_path / "files.tar.gz"
    archive(path, name, kind)
    with pytest.raises(ValueError):
        validate_archive(path)
