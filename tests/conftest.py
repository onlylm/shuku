from __future__ import annotations

import os

# 自动测试不得启动使用真实 SessionLocal 的上传/巡检线程。
os.environ["CLOUD_UPLOAD_WORKER_ENABLED"] = "false"
os.environ["LINK_CHECK_AUTOMATIC_ENABLED"] = "false"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.core.security import hash_password
from app.main import create_app
from app.models import AdminUser, Base, Category, Provider


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with TestingSession() as db:
        db.add_all(
            [
                AdminUser(username="admin", password_hash=hash_password("Testing123!"), display_name="测试管理员"),
                Provider(code="baidu", name="百度网盘", base_domain="pan.baidu.com", sort_order=10),
                Provider(code="quark", name="夸克网盘", base_domain="pan.quark.cn", sort_order=20),
                Category(name="编程开发", slug="programming", sort_order=10),
            ]
        )
        db.commit()
        yield db
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db_session):
    app = create_app()

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin_client(client):
    from bs4 import BeautifulSoup

    page = client.get("/admin/login")
    token = BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf_token"]')["value"]
    response = client.post(
        "/admin/login",
        data={"username": "admin", "password": "Testing123!", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client
