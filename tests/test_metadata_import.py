from sqlalchemy import select

from app.models import Category, Resource
from app.services.metadata_import import (
    commit_meta_preview,
    create_meta_preview,
    split_categories,
)
from app.services.resources import create_resource


def _seed_resource(db, title: str, author: str | None = None, isbn: str | None = None) -> Resource:
    return create_resource(
        db,
        {"title": title, "author": author, "isbn": isbn, "publish_status": "draft"},
    )


def test_split_categories_supports_multiple_separators():
    assert split_categories("心理学、成长,科普") == ["心理学", "成长", "科普"]
    assert split_categories("心理学/成长；科普") == ["心理学", "成长", "科普"]
    assert split_categories("  ") == []


def test_meta_preview_matches_by_isbn_and_fills_fields(db_session):
    db_session.add(Category(name="文学小说", slug="literature")); db_session.commit()
    resource = _seed_resource(db_session, "月亮与六便士", "毛姆", "9787020065512")
    payload = (
        "书名,ISBN,出版社,出版年份,分类\n"
        "月亮与六便士,978-7-02-006551-2,人民文学出版社,2017,文学小说\n"
    ).encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", payload, 1)

    assert batch.ready_rows == 1
    row = batch.rows[0]
    parsed = row.parsed_data
    assert parsed["match"]["type"] == "isbn"
    assert {item["field"] for item in parsed["fill"]} == {"publisher", "publish_year"}

    result = commit_meta_preview(db_session, batch, {row.id})
    assert result.updated == 1
    assert result.created_categories == 0

    db_session.expire_all()
    stored = db_session.get(Resource, resource.id)
    assert stored.publisher == "人民文学出版社"
    assert stored.publish_year == 2017
    assert [category.name for category in stored.categories] == ["文学小说"]


def test_meta_preview_never_creates_unmapped_categories(db_session):
    resource = _seed_resource(db_session, "被讨厌的勇气", "岸见一郎")
    payload = (
        "书名,作者,分类\n被讨厌的勇气,岸见一郎,\"心理学、成长、编程开发\"\n"
    ).encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", payload, 1)

    row = batch.rows[0]
    plans = row.parsed_data["categories"]
    assert plans == []
    assert row.row_status == "warning"
    assert row.parsed_data["category_error"]

    result = commit_meta_preview(db_session, batch, {row.id})
    assert result.created_categories == 0

    db_session.expire_all()
    stored = db_session.get(Resource, resource.id)
    assert stored.categories == []
    assert db_session.scalar(select(Category).where(Category.name == "心理学")) is None


def test_meta_commit_keeps_existing_values_by_default(db_session):
    resource = create_resource(
        db_session,
        {"title": "人类简史", "author": "赫拉利", "publisher": "原出版社", "publish_year": 2011},
    )
    payload = "书名,作者,出版社,出版年份\n人类简史,赫拉利,新出版社,2020\n".encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", payload, 1)

    row = batch.rows[0]
    assert row.row_status == "noop"
    assert row.parsed_data["fill"] == []

    commit_meta_preview(db_session, batch, {row.id})
    db_session.expire_all()
    stored = db_session.get(Resource, resource.id)
    assert stored.publisher == "原出版社"
    assert stored.publish_year == 2011


def test_meta_commit_can_overwrite_when_requested(db_session):
    resource = create_resource(
        db_session,
        {"title": "人类简史", "author": "赫拉利", "publisher": "原出版社"},
    )
    payload = "书名,作者,出版社\n人类简史,赫拉利,新出版社\n".encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", payload, 1)
    row = batch.rows[0]

    commit_meta_preview(db_session, batch, {row.id}, overwrite=True)
    db_session.expire_all()
    stored = db_session.get(Resource, resource.id)
    assert stored.publisher == "新出版社"


def test_meta_preview_marks_unmatched_and_incomplete_rows(db_session):
    payload = "书名,ISBN,作者\n不存在的书,,\n,,某位作者\n".encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", payload, 1)
    assert [row.row_status for row in batch.rows] == ["unmatched", "error"]
    assert "没有找到" in batch.rows[0].message


def test_meta_preview_requires_title_or_isbn_column(db_session):
    import pytest

    with pytest.raises(ValueError):
        create_meta_preview(db_session, "meta.csv", "作者,出版社\n毛姆,人民文学出版社\n".encode("utf-8-sig"), 1)


def test_meta_preview_matches_filename_with_author_suffix(db_session):
    create_resource(db_session, {"title": "12个历史灵魂人物", "author": "南希·津瑟·沃尔沃蒂"})
    payload = (
        "文件名,书名,主分类,子类\n"
        "12个历史灵魂人物 - 南希·津瑟·沃尔沃蒂.epub,12个历史灵魂人物 - 南希·津瑟·沃尔沃蒂,历史文化,世界历史\n"
    ).encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", payload, 1)

    row = batch.rows[0]
    assert row.row_status == "warning"
    assert row.parsed_data["match"]["type"] == "filename"


def test_meta_preview_builds_two_level_categories(db_session):
    parent = Category(name="心理学", slug="psychology")
    db_session.add(parent); db_session.flush()
    db_session.add(Category(name="心理自助", slug="psychology-help", parent_id=parent.id)); db_session.commit()
    resource = create_resource(db_session, {"title": "被讨厌的勇气", "author": "岸见一郎"})
    payload = "书名,作者,主分类,子类\n被讨厌的勇气,岸见一郎,心理学,心理自助\n".encode("utf-8-sig")
    batch = create_meta_preview(db_session, "meta.csv", payload, 1)

    plans = batch.rows[0].parsed_data["categories"]
    assert [(plan["name"], plan["level"], plan["parent"]) for plan in plans] == [
        ("心理学", 1, None),
        ("心理自助", 2, "心理学"),
    ]

    commit_meta_preview(db_session, batch, {batch.rows[0].id})
    db_session.expire_all()
    child = db_session.scalar(select(Category).where(Category.name == "心理自助"))
    assert child is not None
    assert child.parent_id is not None
    assert db_session.get(Category, child.parent_id).name == "心理学"
    assert "心理自助" in {category.name for category in db_session.get(Resource, resource.id).categories}


def test_calibre_export_columns_are_normalized(db_session):
    resource = create_resource(db_session, {"title": "山木方法", "author": "宋山木"})
    resource.language = ""  # 列是 NOT NULL，用空串模拟「还没语言信息」
    db_session.commit()
    payload = (
        "title,authors,publisher,pubdate,languages,formats,isbn\n"
        "山木方法,宋山木,企业管理出版社,2010-01-15T00:00:00+08:00,zho,\"mobi, txt\",9787802553866\n"
    ).encode("utf-8-sig")
    batch = create_meta_preview(db_session, "calibre.csv", payload, 1)

    row = batch.rows[0]
    fill = {item["field"]: item["new"] for item in row.parsed_data["fill"]}
    assert fill["publish_year"] == "2010"
    assert fill["language"] == "zh-CN"
    assert fill["formats"] == "MOBI · TXT"

    commit_meta_preview(db_session, batch, {row.id})
    db_session.expire_all()
    stored = db_session.get(Resource, resource.id)
    assert stored.publish_year == 2010
    assert stored.language == "zh-CN"
    assert stored.formats == "MOBI · TXT"
    assert stored.isbn == "9787802553866"


def test_meta_page_requires_login(client):
    response = client.get("/admin/import/meta", follow_redirects=False)
    assert response.status_code == 303
    assert "/admin/login" in response.headers["location"]


def test_meta_page_renders_for_admin(admin_client):
    response = admin_client.get("/admin/import/meta")
    assert response.status_code == 200
    assert "元数据补全" in response.text
    assert "匹配顺序" in response.text
    assert "主分类 + 子类" in response.text
