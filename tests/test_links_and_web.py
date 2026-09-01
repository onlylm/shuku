from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import ChannelShareLink, LinkClick, Provider, Resource, ResourceChannel, SearchQuery
from app.providers import registry, url_hash
from app.services.links import check_link
from app.services.link_monitor import check_due_links
from app.services.resources import create_resource
from app.services.site_settings import put_value


def _make_link(
    db_session,
    *,
    visible: bool = True,
    status: str = "active",
    title: str = "可见测试书",
    share_id: str = "visible-test",
    author: str | None = "测试作者",
    formats: str | None = "PDF",
):
    provider = db_session.scalar(select(Provider).where(Provider.code == "baidu"))
    resource = create_resource(
        db_session,
        {
            "title": title,
            "author": author,
            "formats": formats,
            "publish_status": "published",
            "copyright_status": "authorized",
            "source_reference": "合成测试授权说明",
            "category_ids": [1],
        },
    )
    channel = ResourceChannel(resource=resource, provider=provider, status="active")
    db_session.add(channel)
    db_session.flush()
    parsed = registry.recognize(f"https://pan.baidu.com/s/{share_id}?pwd=1234")
    link = ChannelShareLink(
        channel=channel,
        provider_id=provider.id,
        provider_share_id=parsed.share_id,
        share_url=parsed.normalized_url,
        normalized_url=parsed.normalized_url,
        normalized_url_hash=url_hash(parsed.normalized_url),
        extract_code=parsed.extract_code,
        is_visible=visible,
        status=status,
    )
    db_session.add(link)
    db_session.commit()
    return resource, link


def test_invalid_check_hides_link(db_session):
    _, link = _make_link(db_session)
    link = db_session.scalar(
        select(ChannelShareLink)
        .where(ChannelShareLink.id == link.id)
        .options(selectinload(ChannelShareLink.provider))
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="啊哦，你来晚了，分享已失效", request=request))
    with httpx.Client(transport=transport) as mock_client:
        log = check_link(db_session, link, mock_client)
    assert log.result == "invalid"
    assert link.is_visible is False
    assert link.status == "invalid"


def test_valid_check_restores_link(db_session):
    _, link = _make_link(db_session, visible=False, status="invalid")
    link = db_session.scalar(
        select(ChannelShareLink)
        .where(ChannelShareLink.id == link.id)
        .options(selectinload(ChannelShareLink.provider))
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="百度网盘分享页面 文件列表", request=request))
    with httpx.Client(transport=transport) as mock_client:
        log = check_link(db_session, link, mock_client)
    assert log.result == "ok"
    assert link.is_visible is True
    assert link.status == "active"


def test_transient_error_only_hides_after_threshold(db_session):
    _, link = _make_link(db_session, share_id="transient-error")
    link = db_session.scalar(
        select(ChannelShareLink)
        .where(ChannelShareLink.id == link.id)
        .options(selectinload(ChannelShareLink.provider))
    )

    def fail(request):
        raise httpx.ConnectError("临时网络中断", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as mock_client:
        first = check_link(db_session, link, mock_client)
        assert first.result == "error"
        assert link.status == "active"
        assert link.is_visible is True

        second = check_link(db_session, link, mock_client)
        assert second.result == "error"
        assert link.status == "error"
        assert link.is_visible is False


def test_due_monitor_checks_pending_links(db_session):
    put_value(db_session, "operations", {"link_check_enabled": True, "link_check_mode": "interval", "link_check_interval_minutes": 360})
    db_session.commit()
    _, link = _make_link(db_session, visible=False, status="pending", share_id="monitor-due")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="百度网盘分享页面 文件列表", request=request)
    )
    with httpx.Client(transport=transport) as mock_client:
        result = check_due_links(db_session, client=mock_client, limit=10)
    assert result.checked == 1
    assert result.ok == 1
    assert link.status == "active"
    assert link.is_visible is True


def test_admin_batch_checks_selected_links(admin_client, db_session, monkeypatch):
    _, first = _make_link(db_session, visible=False, status="pending", share_id="batch-one")
    _, second = _make_link(db_session, visible=False, status="pending", share_id="batch-two")
    import app.admin.routes as routes
    def good(db, link):
        link.status = "active"; link.is_visible = True
        return type("Log", (), {"result": "ok"})()
    monkeypatch.setattr(routes, "check_link", good)
    from bs4 import BeautifulSoup
    token = BeautifulSoup(admin_client.get("/admin/links").text, "html.parser").select_one('[name="csrf_token"]')["value"]
    response = admin_client.post("/admin/links/batch-check", data={"csrf_token": token, "scope": "selected", "link_id": [str(first.id), str(second.id)]})
    assert "批量检测完成" in response.text and "有效 2 条" in response.text
    assert first.is_visible and second.is_visible


def test_frontend_only_shows_valid_links(client, db_session):
    resource, link = _make_link(db_session)
    detail = client.get(f"/book/id/{resource.id}")
    assert detail.status_code == 200
    assert "百度网盘" in detail.text
    link.is_visible = False
    link.status = "invalid"
    db_session.commit()
    hidden = client.get(f"/book/id/{resource.id}")
    assert hidden.status_code == 200
    assert "网盘入口暂不可用" in hidden.text
    assert f'/go/{link.id}' not in hidden.text


def test_frontend_hides_missing_metadata_placeholders(client, db_session):
    from bs4 import BeautifulSoup

    resource, _ = _make_link(
        db_session,
        title="资料精简测试",
        share_id="clean-metadata",
        author=None,
        formats=None,
    )
    detail = client.get(f"/book/id/{resource.id}")
    visible_text = BeautifulSoup(detail.text, "html.parser").get_text(" ", strip=True)
    assert detail.status_code == 200
    assert "待补充" not in visible_text
    assert "详细简介待补充" not in visible_text
    assert "zh-CN" not in visible_text
    assert "中文" in visible_text


def test_frontend_resource_pagination(client, db_session):
    for number in range(25):
        _make_link(
            db_session,
            title=f"分页测试书{number:02d}",
            share_id=f"pagination-{number}",
        )
    first_page = client.get("/books")
    second_page = client.get("/books?page=2")
    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert first_page.text.count('class="book-card"') == 24
    assert second_page.text.count('class="book-card"') == 1
    assert "下一页" in first_page.text
    assert "上一页" in second_page.text


def test_go_records_click_and_invalid_returns_410(client, db_session):
    _, link = _make_link(db_session)
    response = client.get(f"/go/{link.id}", follow_redirects=False)
    assert response.status_code == 302
    assert db_session.scalar(select(LinkClick)) is not None
    link.is_visible = False
    link.status = "invalid"
    db_session.commit()
    response = client.get(f"/go/{link.id}", follow_redirects=False)
    assert response.status_code == 410


def test_zero_result_search_is_recorded(client, db_session):
    response = client.get("/search?q=绝对不存在的书名")
    assert response.status_code == 200
    query = db_session.scalar(select(SearchQuery).order_by(SearchQuery.id.desc()))
    assert query.result_count == 0


def test_seo_endpoints(client, db_session):
    resource, _ = _make_link(db_session)
    assert client.get("/robots.txt").status_code == 200
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "<urlset" in sitemap.text
    assert f"/book/id/{resource.id}" in sitemap.text
    assert "/book/可见测试书" not in sitemap.text
