import json

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import select
from starlette.datastructures import FormData

from app.models import Category, CategoryRedirect, Resource
from app.services.category_forms import category_ids_from_form, category_picker
from app.services.resources import create_resource
from tests.test_fixed_catalog import upgrade, category


def prepare(db):
    roots = upgrade(db)
    science, literature = db.get(Category, roots["science"]), db.get(Category, roots["literature"])
    ai = category(db, "人工智能", "picker-ai", science)
    novel = category(db, "网络与通俗小说", "picker-novel", literature)
    db.commit()
    return science, literature, ai, novel


def make_book(db, cats):
    book = create_resource(db, {"title": "分类选择合成书", "category_ids": [c.id for c in cats],
        "copyright_status": "authorized", "source_reference": "合成测试", "publish_status": "published"})
    db.commit()
    return book


def soup(client, url):
    return BeautifulSoup(client.get(url).text, "html.parser")


def post(client, url, **values):
    page = soup(client, url)
    return client.post(url, data={"csrf_token": page.select_one('[name="csrf_token"]')["value"],
        "category_picker": "1", "title": "分类选择合成书", **values})


def test_picker_compact_prefilled_and_excludes_hidden_merged(admin_client, db_session):
    science, lit, ai, novel = prepare(db_session)
    hidden = category(db_session, "隐藏测试项", "picker-hidden", science, visible=False)
    merged = category(db_session, "合并测试项", "picker-merged", science)
    db_session.add(CategoryRedirect(source_id=merged.id, target_id=ai.id))
    book = make_book(db_session, [lit, novel])
    page = soup(admin_client, f"/admin/resources/{book.id}/edit")
    assert not page.select('input[name="category_ids"]')
    assert len(page.select('[data-category-picker] select')) == 2
    assert len(page.select('[name="main_category_id"] option')) == 9
    assert page.select_one('[name="main_category_id"] option[selected]')["value"] == str(lit.id)
    assert page.select_one('[name="subcategory_id"] option[selected]')["value"] == str(novel.id)
    sub_ids = {o["value"] for o in page.select('[name="subcategory_id"] option')}
    assert str(ai.id) not in sub_ids
    options = json.loads(page.select_one('[data-category-options]').string)
    assert hidden.id not in {o["id"] for o in options}
    assert merged.id not in {o["id"] for o in options}


def test_edit_replaces_path_saves_and_root_only(admin_client, db_session):
    science, lit, ai, novel = prepare(db_session)
    book = make_book(db_session, [science, ai])
    url = f"/admin/resources/{book.id}/edit"
    response = post(admin_client, url, main_category_id=str(lit.id), subcategory_id=str(novel.id))
    assert response.status_code == 200
    db_session.refresh(book)
    assert {c.id for c in book.categories} == {lit.id, novel.id}
    assert book.publish_status == "published"
    post(admin_client, url, main_category_id=str(science.id), subcategory_id="")
    db_session.refresh(book)
    assert {c.id for c in book.categories} == {science.id}
    post(admin_client, url, main_category_id="", subcategory_id="")
    db_session.refresh(book)
    assert book.categories == [] and book.publish_status == "draft"


def test_bad_path_rejected_without_modifying_book(admin_client, db_session):
    science, lit, ai, novel = prepare(db_session)
    book = make_book(db_session, [science, ai])
    response = post(admin_client, f"/admin/resources/{book.id}/edit", title="不应保存",
                    main_category_id=str(science.id), subcategory_id=str(novel.id))
    assert "二级分类不属于所选一级分类" in response.text
    db_session.refresh(book)
    assert book.title == "分类选择合成书"
    assert {c.id for c in book.categories} == {science.id, ai.id}


def test_existing_conflicts_preserved_until_explicit_choice(admin_client, db_session):
    science, lit, ai, novel = prepare(db_session)
    book = make_book(db_session, [science, ai, lit, novel])
    url = f"/admin/resources/{book.id}/edit"
    page = soup(admin_client, url)
    assert page.select_one('[name="main_category_id"] option[selected]')["value"] == "__keep__"
    post(admin_client, url, main_category_id="__keep__", subcategory_id="")
    db_session.refresh(book)
    assert {c.id for c in book.categories} == {science.id, ai.id, lit.id, novel.id}
    post(admin_client, url, main_category_id=str(lit.id), subcategory_id=str(novel.id))
    db_session.refresh(book)
    assert {c.id for c in book.categories} == {lit.id, novel.id}


def test_new_form_error_keeps_selection_and_can_save(admin_client, db_session):
    science, lit, ai, novel = prepare(db_session)
    response = post(admin_client, "/admin/resources/new", title="", main_category_id=str(lit.id), subcategory_id=str(novel.id))
    assert response.status_code == 400
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one('[name="main_category_id"] option[selected]')["value"] == str(lit.id)
    assert page.select_one('[name="subcategory_id"] option[selected]')["value"] == str(novel.id)
    assert db_session.scalar(select(Resource)) is None
    response = post(admin_client, "/admin/resources/new", main_category_id=str(lit.id), subcategory_id=str(novel.id))
    assert response.status_code == 200
    book = db_session.scalar(select(Resource))
    assert {c.id for c in book.categories} == {lit.id, novel.id}


def test_hidden_old_path_not_cleared_and_new_form_cannot_keep(db_session):
    science, lit, ai, novel = prepare(db_session)
    book = make_book(db_session, [science, ai])
    ai.is_visible = False; db_session.commit()
    assert category_picker(db_session, book)["keep"]
    kept = category_ids_from_form(db_session, FormData({"category_picker": "1", "main_category_id": "__keep__"}), book)
    assert set(kept) == {science.id, ai.id}
    with pytest.raises(ValueError):
        category_ids_from_form(db_session, FormData({"category_picker": "1", "main_category_id": "__keep__"}))
    with pytest.raises(ValueError):
        category_ids_from_form(db_session, FormData({"category_picker": "1", "main_category_id": str(science.id), "subcategory_id": str(ai.id)}))
    assert category_ids_from_form(db_session, FormData([("category_ids", "1"), ("category_ids", "2")])) == ["1", "2"]


def test_duplicate_parameters_rejected(db_session):
    with pytest.raises(ValueError):
        category_ids_from_form(db_session, FormData([("category_picker", "1"), ("main_category_id", "1"), ("main_category_id", "2")]))
