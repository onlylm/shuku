import json
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models import Resource
from app.services.resources import create_resource, resource_public_url, update_resource


def book(db, title="固定地址测试书", **values):
    resource = create_resource(db, {
        "title": title, "author": "测试作者", "description": "用于验证固定网址的合成资料。",
        "copyright_status": "authorized", "source_reference": "合成测试授权说明",
        "category_ids": [1], "publish_status": "published", **values,
    })
    db.commit()
    return resource


def test_legacy_url_redirects_once_without_changing_book_or_count(client, db_session):
    resource = book(db_session, title="AI 未来进行式")
    original = (resource.id, resource.slug, resource.resource_code, resource.author, resource.description)
    legacy = f"/book/{quote(resource.slug)}"
    path = f"/book/id/{resource.id}"
    response = client.get(legacy, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == path
    db_session.refresh(resource)
    assert resource.view_count == 0
    followed = client.get(legacy)
    assert [r.status_code for r in followed.history] == [301]
    assert followed.status_code == 200 and followed.url.path == path
    db_session.refresh(resource)
    assert resource.view_count == 1
    assert (resource.id, resource.slug, resource.resource_code, resource.author, resource.description) == original


def test_title_and_category_changes_keep_both_urls_working(client, db_session):
    resource = book(db_session, title="旧书名带副标题")
    legacy, current = f"/book/{resource.slug}", f"/book/id/{resource.id}"
    update_resource(db_session, resource, {"title": "新的简洁书名", "author": "修正作者", "description": "新的简介", "category_ids": [1]})
    db_session.commit()
    assert client.get(legacy, follow_redirects=False).headers["location"] == current
    page = client.get(current)
    soup = BeautifulSoup(page.text, "html.parser")
    assert page.status_code == 200 and soup.h1.get_text() == "新的简洁书名"
    assert "修正作者" in soup.get_text() and "新的简介" in soup.get_text()
    assert db_session.scalar(select(func.count(Resource.id))) == 1


def test_numeric_legacy_title_is_never_interpreted_as_id(client, db_session):
    original = book(db_session, title="按ID打开的书")
    numeric_title = book(db_session, title=str(original.id))
    assert original.id != numeric_title.id
    old = client.get(f"/book/{original.id}", follow_redirects=False)
    assert old.status_code == 301 and old.headers["location"] == f"/book/id/{numeric_title.id}"
    assert BeautifulSoup(client.get(f"/book/id/{original.id}").text, "html.parser").h1.get_text() == original.title
    assert BeautifulSoup(client.get(f"/book/{numeric_title.slug}").text, "html.parser").h1.get_text() == numeric_title.title


def test_legacy_title_id_does_not_conflict_with_namespace(client, db_session):
    resource = book(db_session, title="id")
    assert client.get("/book/id", follow_redirects=False).headers["location"] == f"/book/id/{resource.id}"
    assert client.get(f"/book/id/{resource.id}").status_code == 200


def test_book_metadata_and_sitemap_use_same_configured_id_url(client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", "https://books.example.test")
    resource = book(db_session)
    expected = f"https://books.example.test/book/id/{resource.id}"
    response = client.get(f"/book/id/{resource.id}?ref=old-share", headers={"Host": "untrusted.example.test"})
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('link[rel="canonical"]')["href"] == expected
    structured = json.loads(soup.select_one('script[type="application/ld+json"]').string)
    assert structured["url"] == expected and structured["name"] == resource.title
    sitemap = ElementTree.fromstring(client.get("/sitemap.xml").content)
    urls = [entry.text for entry in sitemap.findall("{*}url/{*}loc")]
    assert urls.count(expected) == 1
    assert all(f"/book/{resource.slug}" not in url for url in urls)


@pytest.mark.parametrize("page", ["/books", "/search?q=固定地址测试书", "/category/programming"])
def test_cards_link_directly_to_id_pages(client, db_session, page):
    resource = book(db_session)
    soup = BeautifulSoup(client.get(page).text, "html.parser")
    cards = soup.select("a.book-card")
    assert len(cards) == 1
    assert urlsplit(cards[0]["href"]).path == f"/book/id/{resource.id}"


def test_search_api_adds_id_url_without_removing_legacy_slug(client, db_session):
    resource = book(db_session)
    result = client.get("/api/v1/resources/search?q=固定地址测试书").json()
    assert result["items"][0]["id"] == resource.id
    assert result["items"][0]["slug"] == resource.slug
    assert result["items"][0]["detail_url"] == resource_public_url(resource.id)


def test_admin_view_frontend_and_edit_use_permanent_url(admin_client, db_session):
    resource = book(db_session)
    page = admin_client.get(f"/admin/resources/{resource.id}/edit")
    soup = BeautifulSoup(page.text, "html.parser")
    view = soup.select_one('.heading-actions a[target="_blank"]')
    assert urlsplit(view["href"]).path == f"/book/id/{resource.id}"
    csrf = soup.select_one('input[name="csrf_token"]')["value"]
    old_slug = resource.slug
    response = admin_client.post(f"/admin/resources/{resource.id}/edit", data={
        "csrf_token": csrf, "title": "后台改好的书名", "copyright_status": "authorized",
        "source_reference": "合成测试授权说明", "category_ids": "1", "publish_status": "published",
        "metadata_locked": "1", "submit_action": "save",
    })
    assert response.status_code == 200
    db_session.refresh(resource)
    assert resource.slug == old_slug
    assert BeautifulSoup(admin_client.get(f"/book/id/{resource.id}").text, "html.parser").h1.get_text() == "后台改好的书名"


@pytest.mark.parametrize("status", ["draft", "archived"])
def test_unpublished_books_are_hidden_on_both_addresses(client, db_session, status):
    resource = book(db_session, publish_status=status)
    for path in (f"/book/id/{resource.id}", f"/book/{resource.slug}"):
        for method in (client.get, client.head):
            response = method(path, follow_redirects=False)
            assert response.status_code == 404 and "location" not in response.headers
    assert f"/book/id/{resource.id}" not in client.get("/sitemap.xml").text


@pytest.mark.parametrize("path", ["/book/id/0", "/book/id/-1", "/book/id/abc", "/book/id/1.0", "/book/id/999999999999999999999", "/book/id/999999", "/book/不存在的旧别名"])
def test_bad_ids_and_unknown_aliases_return_404(client, path):
    assert client.get(path, follow_redirects=False).status_code == 404


def test_head_and_leading_zero_urls_do_not_inflate_views(client, db_session):
    resource = book(db_session)
    path = f"/book/id/{resource.id}"
    leading_zero = f"/book/id/00{resource.id}"
    response = client.get(leading_zero, follow_redirects=False)
    assert response.status_code == 301 and response.headers["location"] == path
    head = client.head(path, follow_redirects=False)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-type"].startswith("text/html")
    assert int(head.headers["content-length"]) > 0
    legacy_head = client.head(f"/book/{resource.slug}", follow_redirects=False)
    assert legacy_head.status_code == 301 and legacy_head.headers["location"] == path
    db_session.refresh(resource)
    assert resource.view_count == 0


def test_deleted_book_id_is_not_reused_for_new_book(client, db_session):
    removed = book(db_session, title="准备删除的书")
    removed_id, removed_slug = removed.id, removed.slug
    db_session.delete(removed); db_session.commit()
    fresh = book(db_session, title="后来加入的另一部书")
    assert fresh.id > removed_id
    assert client.get(f"/book/id/{removed_id}", follow_redirects=False).status_code == 404
    assert client.get(f"/book/{removed_slug}", follow_redirects=False).status_code == 404
    assert client.get(f"/book/id/{fresh.id}").status_code == 200
