from sqlalchemy import select

from app.models import ChannelShareLink, ImportBatch, Resource
from app.services.imports import commit_preview, create_preview


def test_import_preview_detects_duplicate_in_same_batch(db_session):
    payload = (
        "书名,作者,网盘链接,提取码\n"
        "第一本书,张三,https://pan.baidu.com/s/same-share,1111\n"
        "第二本书,李四,https://pan.baidu.com/s/same-share,2222\n"
    ).encode("utf-8-sig")
    batch = create_preview(db_session, "demo.csv", payload, 1)
    assert batch.ready_rows == 1
    assert batch.duplicate_rows == 1
    assert [row.row_status for row in batch.rows] == ["ready", "duplicate_batch"]


def test_import_commit_creates_hidden_pending_link(db_session):
    payload = "书名,作者,分类,网盘链接\nPython测试书,测试作者,编程开发,https://pan.quark.cn/s/new-book\n".encode("utf-8-sig")
    batch = create_preview(db_session, "demo.csv", payload, 1)
    selected = {batch.rows[0].id}
    result = commit_preview(db_session, batch, selected)
    assert result.committed == 1
    resource = db_session.scalar(select(Resource).where(Resource.title == "Python测试书"))
    link = db_session.scalar(select(ChannelShareLink))
    assert resource is not None
    assert resource.publish_status == "draft"
    assert resource.copyright_status == "pending"
    assert link.status == "pending"
    assert link.is_visible is False


def test_import_commit_saves_optional_book_metadata(db_session):
    payload = (
        "书名,副标题,作者,ISBN,出版社,出版年份,分类,格式,语言,简介,版权状态,授权说明,网盘链接\n"
        "批量资料测试,测试副标题,测试作者,9787300000001,测试出版社,2024,历史人文,PDF,zh-CN,"
        "这是批量导入的简介,public_domain,公版来源说明,https://pan.quark.cn/s/metadata-book\n"
    ).encode("utf-8-sig")
    batch = create_preview(db_session, "metadata.csv", payload, 1)
    result = commit_preview(db_session, batch, {batch.rows[0].id})

    assert result.committed == 1
    resource = db_session.scalar(select(Resource).where(Resource.title == "批量资料测试"))
    assert resource is not None
    assert resource.subtitle == "测试副标题"
    assert resource.author == "测试作者"
    assert resource.publisher == "测试出版社"
    assert resource.publish_year == 2024
    assert resource.formats == "PDF"
    assert resource.language == "zh-CN"
    assert resource.description == "这是批量导入的简介"
    assert resource.copyright_status == "public_domain"
    assert resource.source_reference == "公版来源说明"


def test_existing_link_on_other_title_is_conflict(db_session):
    first_payload = "书名,网盘链接\n第一本书,https://pan.baidu.com/s/existing\n".encode("utf-8-sig")
    first = create_preview(db_session, "one.csv", first_payload, 1)
    commit_preview(db_session, first, {first.rows[0].id})
    second_payload = "书名,网盘链接\n完全不同的书,https://pan.baidu.com/s/existing?pwd=zzzz\n".encode("utf-8-sig")
    second = create_preview(db_session, "two.csv", second_payload, 1)
    assert second.conflict_rows == 1
    assert second.rows[0].row_status == "conflict"
    assert "禁止导入" in second.rows[0].message


def test_import_page_requires_login(client):
    response = client.get("/admin/import", follow_redirects=False)
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


def test_admin_upload_preview(admin_client):
    from bs4 import BeautifulSoup

    page = admin_client.get("/admin/import")
    token = BeautifulSoup(page.text, "html.parser").select_one('input[name="csrf_token"]')["value"]
    payload = "书名,网盘链接\n网页导入测试,https://pan.quark.cn/s/web-import\n".encode("utf-8-sig")
    response = admin_client.post(
        "/admin/import",
        data={"csrf_token": token},
        files={"file": ("demo.csv", payload, "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/admin/import/" in response.headers["location"]
