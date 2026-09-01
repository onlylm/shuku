from __future__ import annotations

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.models import Category, ChannelShareLink, Resource
from app.services.links import add_or_replace_link
from app.services.resources import create_resource


def _csrf(response) -> str:
    return BeautifulSoup(response.text, "html.parser").select_one('input[name="csrf_token"]')["value"]


def test_create_resource_with_initial_link_and_duplicate_guard(admin_client, db_session):
    page = admin_client.get("/admin/resources/new")
    response = admin_client.post(
        "/admin/resources/new",
        data={
            "csrf_token": _csrf(page),
            "title": "带首个链接的资源",
            "copyright_status": "authorized",
            "publish_status": "draft",
            "share_url": "https://pan.baidu.com/s/first-link?pwd=9abc",
            "extract_code": "9abc",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    resource = db_session.scalar(select(Resource).where(Resource.title == "带首个链接的资源"))
    link = db_session.scalar(select(ChannelShareLink))
    assert resource is not None
    assert link is not None
    assert link.status == "pending"
    assert link.is_visible is False
    assert link.extract_code == "9abc"

    page = admin_client.get("/admin/resources/new")
    duplicate = admin_client.post(
        "/admin/resources/new",
        data={
            "csrf_token": _csrf(page),
            "title": "重复链接资源",
            "copyright_status": "authorized",
            "publish_status": "draft",
            "share_url": "https://pan.baidu.com/s/first-link?pwd=9abc",
        },
    )
    assert duplicate.status_code == 400
    assert "该链接已被" in duplicate.text
    assert db_session.scalar(select(Resource).where(Resource.title == "重复链接资源")) is None


def test_categories_can_be_created_edited_and_safely_deleted(admin_client, db_session):
    parent = db_session.scalar(select(Category).where(Category.slug == "programming"))
    page = admin_client.get("/admin/categories")
    created = admin_client.post(
        "/admin/categories",
        data={
            "csrf_token": _csrf(page),
            "name": "编程语言",
            "slug": "languages",
            "parent_id": str(parent.id),
            "description": "编程语言教程",
            "sort_order": "5",
            "is_visible": "1",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    category = db_session.scalar(select(Category).where(Category.slug == "languages"))
    assert category.parent_id == parent.id
    assert category.is_visible is True

    edit_page = admin_client.get(f"/admin/categories/{category.id}/edit")
    updated = admin_client.post(
        f"/admin/categories/{category.id}/edit",
        data={
            "csrf_token": _csrf(edit_page),
            "name": "开发语言",
            "slug": "dev-languages",
            "parent_id": "",
            "description": "新的说明",
            "sort_order": "2",
            "is_visible": "1",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    db_session.refresh(category)
    assert category.name == "开发语言"
    assert category.parent_id is None
    assert category.sort_order == 2

    resource = create_resource(
        db_session,
        {
            "title": "分类删除保护测试",
            "publish_status": "draft",
            "copyright_status": "authorized",
            "category_ids": [str(category.id)],
        },
    )
    db_session.commit()
    edit_page = admin_client.get(f"/admin/categories/{category.id}/edit")
    blocked = admin_client.post(
        f"/admin/categories/{category.id}/delete",
        data={"csrf_token": _csrf(edit_page)},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert db_session.get(Category, category.id) is not None
    assert resource.categories[0].id == category.id


def test_new_public_pages_and_chinese_labels(client, admin_client, db_session):
    resource = create_resource(
        db_session,
        {"title": "中文状态测试", "publish_status": "published", "copyright_status": "authorized"},
    )
    db_session.commit()
    resources = admin_client.get("/admin/resources")
    assert "已发布" in resources.text
    assert ">published<" not in resources.text

    assert client.get("/books").status_code == 200
    disclaimer = client.get("/disclaimer")
    assert disclaimer.status_code == 200
    assert "不以免责声明替代必要的版权核验" in disclaimer.text
    assert "/disclaimer" in client.get("/sitemap.xml").text
    assert resource.id is not None


def test_draft_has_clear_publish_and_delete_actions(admin_client, db_session):
    resource = create_resource(
        db_session,
        {"title": "发布删除按钮测试", "publish_status": "draft", "copyright_status": "authorized"},
    )
    db_session.commit()
    page = admin_client.get(f"/admin/resources/{resource.id}/edit")
    assert "保存并发布" in page.text
    assert "永久删除资源" in page.text

    published = admin_client.post(
        f"/admin/resources/{resource.id}/edit",
        data={
            "csrf_token": _csrf(page),
            "title": resource.title,
            "copyright_status": "authorized",
            "publish_status": "draft",
            "submit_action": "publish",
            "source_reference": "合成测试授权说明",
            "category_ids": "1",
        },
        follow_redirects=False,
    )
    assert published.status_code == 303
    db_session.refresh(resource)
    assert resource.publish_status == "published"

    page = admin_client.get(f"/admin/resources/{resource.id}/edit")
    deleted = admin_client.post(
        f"/admin/resources/{resource.id}/delete",
        data={"csrf_token": _csrf(page)},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert db_session.get(Resource, resource.id) is None


def test_dashboard_cards_link_to_details(admin_client):
    dashboard = admin_client.get("/admin")
    assert "/admin/resources" in dashboard.text
    assert "status=problem" in dashboard.text
    assert "/admin/analytics" in dashboard.text
    analytics = admin_client.get("/admin/analytics")
    assert analytics.status_code == 200
    assert "最近跳转" in analytics.text


def test_admin_resource_list_is_paginated(admin_client, db_session):
    for number in range(51):
        create_resource(
            db_session,
            {
                "title": f"后台分页测试{number:02d}",
                "publish_status": "draft",
                "copyright_status": "authorized",
            },
        )
    db_session.commit()

    first = BeautifulSoup(admin_client.get("/admin/resources").text, "html.parser")
    second = BeautifulSoup(admin_client.get("/admin/resources?page=2").text, "html.parser")
    # 页面顶部还有「最近编辑」表格，分页断言只针对资源列表那张表
    first_rows = first.select("table.recent-table")[-1].select("tbody tr")
    second_rows = second.select("table.recent-table")[-1].select("tbody tr")
    assert len(first_rows) == 50
    assert len(second_rows) == 1
    assert "共 51 条" in first.get_text(" ", strip=True)


def test_admin_links_shows_monitor_status(admin_client, db_session):
    resource = create_resource(
        db_session,
        {"title": "巡检状态测试", "publish_status": "published", "copyright_status": "authorized"},
    )
    link = add_or_replace_link(db_session, resource.id, "https://pan.baidu.com/s/monitor-status")
    db_session.commit()

    page = admin_client.get("/admin/links")
    assert page.status_code == 200
    assert "自动巡检未启用" in page.text
    assert "当前有 0 条等待巡检" in page.text
    assert link.normalized_url in page.text


def test_batch_publish_checks_requirements_and_batch_draft(admin_client, db_session):
    valid = create_resource(db_session, {"title": "可以批量发布", "publish_status": "draft",
        "copyright_status": "authorized", "source_reference": "合成测试授权说明", "category_ids": [1]})
    invalid = create_resource(db_session, {"title": "资料不完整", "publish_status": "draft"})
    link = add_or_replace_link(db_session, valid.id, "https://pan.baidu.com/s/batch-publish-valid")
    link.status = "active"; link.is_visible = True
    db_session.commit()
    page = admin_client.get("/admin/resources")
    token = BeautifulSoup(page.text, "html.parser").select_one('[name="csrf_token"]')["value"]
    response = admin_client.post("/admin/resources/batch-status", data={"csrf_token": token, "action": "publish",
        "selected_resource": [str(valid.id), str(invalid.id)]})
    assert "成功 1 条" in response.text and "未通过发布检查 1 条" in response.text
    db_session.refresh(valid); db_session.refresh(invalid)
    assert valid.publish_status == "published" and invalid.publish_status == "draft"
    token = BeautifulSoup(response.text, "html.parser").select_one('[name="csrf_token"]')["value"]
    admin_client.post("/admin/resources/batch-status", data={"csrf_token": token, "action": "draft", "selected_resource": str(valid.id)})
    db_session.refresh(valid)
    assert valid.publish_status == "draft"
