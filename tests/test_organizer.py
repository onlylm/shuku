from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from PIL import Image
from botocore.exceptions import ClientError

from ebook_organizer.connections import Credentials, upload_book, upload_cover
from ebook_organizer.engine import export_snapshot, scan
from ebook_organizer.epub import Limits, inspect_epub, isbn_valid
from ebook_organizer.safeio import Cancelled, Control, safe_name, sha256_file
from ebook_organizer.workspace import Workspace


def make_epub(path, title="测试图书", body="测试正文", cover=True, extra=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = io.BytesIO(); Image.new("RGB", (100, 150), "#347152").save(image, "JPEG")
    opf = f'''<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>测试作者</dc:creator><dc:identifier>9780306406157</dc:identifier></metadata><manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>{'<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>' if cover else ''}</manifest><spine><itemref idref="body"/></spine></package>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/content.opf"/></rootfiles></container>')
        archive.writestr("OPS/content.opf", opf)
        archive.writestr("OPS/body.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + '</body></html>')
        if cover:
            archive.writestr("OPS/cover.jpg", image.getvalue())
        if extra:
            for name, value in extra.items():
                archive.writestr(name, value)
    return path


@pytest.fixture
def library(tmp_path):
    source = tmp_path / "source"
    book = make_epub(source / "文学小说" / "中国文学" / "测试 - 作者.epub")
    ws = Workspace(tmp_path / "workspace")
    scan(ws, source)
    # 后续上传测试明确模拟人工确认，不再把目录识别当作确认。
    ws.edit(ws.books()[0]['book_id'], {'classification_status':'confirmed'})
    return ws, source, book


def test_isbn_validation():
    assert isbn_valid("978-0-306-40615-7") == "9780306406157"
    assert isbn_valid("0-306-40615-2") == "0306406152"
    assert isbn_valid("9780306406158") is None
    assert isbn_valid("urn:isbn:9780306406157") == "9780306406157"


def test_scan_identity_and_source_immutable(library):
    ws, source, path = library
    before = path.stat(); checksum = sha256_file(path)
    old = ws.books()[0]
    moved = path.with_name("移动后名字.epub"); path.rename(moved)
    scan(ws, source)
    assert len(ws.books()) == 1
    assert ws.books()[0]["book_id"] == old["book_id"]
    assert sha256_file(moved) == checksum
    assert moved.stat().st_mtime_ns == before.st_mtime_ns
    assert ws.books()[0]["metadata"]["main_category"] == "文学小说"
    assert ws.books()[0]["metadata"]["isbn"] == "9780306406157"


def test_different_versions_not_merged(library):
    ws, source, _ = library
    make_epub(source / "另一个版本.epub", body="不同版本")
    scan(ws, source)
    assert len(ws.books()) == 2
    assert len({b["book_id"] for b in ws.books()}) == 2


def test_manual_fields_lock_and_undo(library):
    ws, source, _ = library
    book_id = ws.books()[0]["book_id"]
    ws.edit(book_id, {"author": "人工作者", "main_category": "社会科学"})
    scan(ws, source)
    assert ws.book(book_id)["metadata"]["author"] == "人工作者"
    ws.undo(book_id)
    assert ws.book(book_id)["metadata"]["author"] == "测试作者"


def test_export_and_backup(library, tmp_path):
    ws, source, path = library
    book = ws.books()[0]
    exported = export_snapshot(ws, [book["book_id"]], tmp_path / "output")
    data = json.loads((exported / "数据/books.json").read_text("utf-8"))
    copy = exported / data["books"][0]["epub_path"]
    assert sha256_file(copy) == sha256_file(path)
    assert str(source) not in json.dumps(data, ensure_ascii=False)
    assert (exported / "数据/manifest.json").exists()
    assert not list(exported.rglob("*.part"))
    with Image.open(ws.root / book["cover_path"]) as cover:
        assert cover.size == (600, 900)
    ws.backup(tmp_path / "backup.sqlite3")
    assert (tmp_path / "backup.sqlite3").stat().st_size > 0
    with pytest.raises(ValueError):
        export_snapshot(ws, [book["book_id"]], source)


def test_cancel_has_no_final_export(library, tmp_path):
    ws, _, _ = library
    control = Control(); control.cancelled.set()
    with pytest.raises(Cancelled):
        export_snapshot(ws, [ws.books()[0]["book_id"]], tmp_path / "out", control)
    assert not list((tmp_path / "out").glob("export_*"))


@pytest.mark.parametrize("extra,code", [({"../escape": "x"}, "UNSAFE_PATH"), ({"META-INF/encryption.xml": '<encryption><EncryptionMethod Algorithm="unknown"/></encryption>'}, "EPUB_ENCRYPTED")])
def test_unsafe_epub(tmp_path, extra, code):
    path = make_epub(tmp_path / "bad.epub", extra=extra)
    result = inspect_epub(path)
    assert result.status == "blocked"
    assert any(i["code"] == code for i in result.issues)


def test_missing_cover_not_corruption(tmp_path):
    result = inspect_epub(make_epub(tmp_path / "plain.epub", cover=False))
    assert result.status == "warning"


def test_limits_not_corruption(tmp_path):
    result = inspect_epub(make_epub(tmp_path / "limit.epub"), limits=Limits(expanded_bytes=20))
    assert result.status == "blocked"
    assert result.issues[0]["code"] == "LIMIT_EXCEEDED"


def test_source_workspace_overlap(library):
    ws, _, _ = library
    with pytest.raises(ValueError):
        scan(ws, ws.root)


def test_safe_names():
    assert safe_name("CON") == "_CON"
    assert safe_name("中:文?. ") == "中_文_"


class FakeS3:
    def __init__(self):
        self.items, self.puts = {}, 0

    def head_object(self, Bucket, Key):
        if Key not in self.items:
            raise ClientError({"Error": {"Code": "404"}}, "head_object")
        data = self.items[Key]
        return {"ContentLength": len(data["Body"]), "Metadata": data["Metadata"]}

    def put_object(self, **kwargs):
        self.puts += 1; self.items[kwargs["Key"]] = kwargs


def test_cover_upload_public_verify_and_retry(library):
    ws, _, _ = library; book = ws.books()[0]; s3 = FakeS3()
    cfg = {"r2_account": "a" * 32, "r2_bucket": "covers", "r2_public": "https://img.example.com"}
    credentials = Credentials(ws.setting("workspace_id"))
    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(ValueError):
        upload_cover(ws, book["book_id"], cfg, credentials, s3=s3, http=bad)
    assert ws.result(book["book_id"], "r2")["state"] == "uploaded"
    good = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=(ws.root / book["cover_path"]).read_bytes())))
    result = upload_cover(ws, book["book_id"], cfg, credentials, s3=s3, http=good)
    assert result["state"] == "verified" and s3.puts == 1
    bad.close(); good.close()


def test_quark_directory_and_share_retry(library):
    ws, _, _ = library; book = ws.books()[0]
    ws.edit(book["book_id"], {"rights_review_status": "confirmed", "copyright_status": "public_domain", "source_reference": "合成测试资料"})
    class Connector:
        def __init__(self): self.commands = []; self.fail = True
        def _run(self, args, **kwargs):
            self.commands.append(args)
            if args[0] == "create-folder": return {"data": {"fid": "folder-" + args[2]}}
            if args[0] == "upload": return {"data": {"fids": ["file-123"]}}
            if self.fail: self.fail = False; raise ValueError("模拟分享失败")
            return {"data": {"share_url": "https://pan.quark.cn/s/testshare"}}
    connector = Connector(); credentials = Credentials("test")
    with pytest.raises(ValueError):
        upload_book(ws, book["book_id"], "quark", {"quark_parent": "0"}, credentials, connector=connector)
    result = upload_book(ws, book["book_id"], "quark", {"quark_parent": "0"}, credentials, connector=connector)
    assert result["share_url"]
    assert len([c for c in connector.commands if c[0] == "upload"]) == 1
    assert len([c for c in connector.commands if c[0] == "create-folder"]) == 2
    assert "--parent-fid" in next(c for c in connector.commands if c[0] == "upload")


def test_unknown_upload_is_not_blindly_retried(library):
    ws, _, _ = library; book = ws.books()[0]
    ws.edit(book["book_id"], {"rights_review_status": "confirmed", "copyright_status": "public_domain", "source_reference": "合成测试"})
    ws.save_result(book["book_id"], "quark", {"state": "uploading"})
    with pytest.raises(ValueError, match="上次上传结果未确认"):
        upload_book(ws, book["book_id"], "quark", {}, Credentials("test"))


def test_backup_archive_restore_identity_and_cover(library, tmp_path):
    from ebook_organizer.maintenance import backup_workspace, restore_workspace
    ws, _, _ = library
    archive = backup_workspace(ws, tmp_path / "backup.zip")
    target = restore_workspace(archive, tmp_path / "restored")
    restored = Workspace(target)
    assert restored.books()[0]["book_id"] == ws.books()[0]["book_id"]
    assert (restored.root / restored.books()[0]["cover_path"]).exists()
    with pytest.raises(ValueError):
        restore_workspace(archive, ws.root)


def test_missing_body_reference_fails(tmp_path):
    path = make_epub(tmp_path / "missing.epub", body='<img src="missing.jpg"/>')
    assert inspect_epub(path).status == "failed"


def test_body_fragment_reference_allowed(tmp_path):
    path = make_epub(tmp_path / "fragment.epub", body='<a href="#note">注释</a><p id="note">正文</p>')
    assert inspect_epub(path).status == "passed"


def test_site_category_mapping_is_separate_from_local(library):
    from ebook_organizer.engine import public_record
    ws, _, _ = library
    ws.set_setting("site_category_mapping", {"文学小说/中国文学": ["网站文学", "现代文学"]})
    book = ws.books()[0]
    assert public_record(ws, book)["main_category"] == "网站文学"
    assert book["metadata"]["main_category"] == "文学小说"


def test_broken_hyperlink_is_warning_not_missing_body(tmp_path):
    result = inspect_epub(make_epub(tmp_path / "link.epub", body='<a href="missing.html">引用</a>'))
    assert result.status == "warning"
    assert result.issues[0]["code"] == "BROKEN_HYPERLINK"


def test_xml_doctype_is_inert_and_entities_blocked():
    from ebook_organizer.epub import xml
    from defusedxml.common import DefusedXmlException
    assert xml(b'<!DOCTYPE html PUBLIC "unused" "https://example.invalid/external.dtd"><html/>').tag == "html"
    with pytest.raises(DefusedXmlException):
        xml(b'<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]><html>&x;</html>')


def test_font_algorithm_cannot_mask_body_encryption(tmp_path):
    extra = {"META-INF/encryption.xml": '<encryption><EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/><CipherReference URI="OPS/body.xhtml"/></encryption>'}
    result = inspect_epub(make_epub(tmp_path / "body.epub", extra=extra))
    assert result.status == "blocked"
    assert result.issues[0]["code"] == "EPUB_ENCRYPTED"


def test_manifest_covers_readme_and_export_disallows_workspace(library, tmp_path):
    ws, _, _ = library
    ids = [ws.books()[0]["book_id"]]
    with pytest.raises(ValueError):
        export_snapshot(ws, ids, ws.root / "output")
    target = export_snapshot(ws, ids, tmp_path / "exports")
    manifest = json.loads((target / "数据/manifest.json").read_text("utf-8"))
    assert any(row["path"] == "README.txt" for row in manifest["files"])
    for row in manifest["files"]:
        assert sha256_file(target / row["path"]) == row["sha256"]


def test_website_bad_fields_do_not_break_offline_export(library, tmp_path):
    ws, _, _ = library
    book_id = ws.books()[0]["book_id"]
    ws.edit(book_id, {"publisher": "超长" * 256})
    ws.set_setting("connections", {"site_id": "jingye-local"})
    target = export_snapshot(ws, [book_id], tmp_path / "exports")
    assert (target / "异常区/site-fields.json").exists()
    assert len(list(target.rglob("*.epub"))) == 1


def test_site_client_commits_one_at_a_time_and_saves_receipts(library):
    from ebook_organizer.connections import SiteClient
    ws, _, _ = library
    requests = []
    def handler(request):
        data = json.loads(request.content)
        requests.append(data)
        assert len(data["choices"]) == 1
        bid = data["choices"][0]["book_id"]
        return httpx.Response(200, json={"site_id": "testing", "items": {bid: {"status": "ok"}}})
    credentials = Credentials("testing-session-only"); credentials.memory["site_token"] = "not-a-real-secret"
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = SiteClient(ws, {"site_url": "http://127.0.0.1", "site_id": "testing"}, credentials, client=http)
    client.commit({"export_id": "a" * 32}, [{"book_id": "one", "action": "create"}, {"book_id": "two", "action": "create"}])
    assert len(requests) == 2
    assert ws.result("one", "site:testing")["status"] == "ok"
    http.close()


def test_remove_books_is_local_backed_up_and_atomic(library):
    import sqlite3
    ws, _, source = library
    book = ws.books()[0]; bid = book["book_id"]
    checksum = sha256_file(source)
    ws.save_result(bid, "quark", {"share_url": "https://pan.quark.cn/s/local-test"})
    ws.edit(bid, {"author": "编辑过的作者"})
    ws.set_setting("last_pipeline_task", {"book_ids": [bid]})
    result = ws.delete_books([bid, bid])
    assert result["deleted"] == 1 and not ws.books()
    assert sha256_file(source) == checksum
    assert (ws.root / book["cover_path"]).exists()
    assert not ws.result(bid, "quark")
    assert ws.setting("last_pipeline_task") is None
    with sqlite3.connect(result["backup"]) as db:
        assert db.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    assert ws.delete_books([])["deleted"] == 0
    with pytest.raises(ValueError):
        ws.delete_books(bid)


def test_remove_cancel_rolls_back_and_running_job_blocks(library):
    ws, _, _ = library
    bid = ws.books()[0]["book_id"]
    class Stop(Control):
        def __init__(self): super().__init__(); self.calls = 0
        def check(self):
            self.calls += 1
            if self.calls == 3: raise Cancelled()
    with pytest.raises(Cancelled):
        ws.delete_books([bid, "missing"], Stop())
    assert ws.book(bid)
    job = ws.start_job("scan", {})
    with pytest.raises(ValueError, match="运行中"):
        ws.delete_books([bid])
    ws.finish_job(job, "cancelled", {})
    assert ws.delete_books([bid])["deleted"] == 1


def test_table_numeric_isbn_empty_rows_and_duplicate_versions(library):
    from ebook_organizer.table_import import preview_updates, apply_updates
    ws, source, _ = library
    rows = [["ISBN", "出版社", "出版年份"], [], [9780306406157, "新出版社", 2020.0], [None]]
    preview = preview_updates(ws.books(), rows)
    assert preview["total"] == 1 and len(preview["updates"]) == 1
    assert apply_updates(ws, preview) == 1
    book = ws.books()[0]
    assert book["metadata"]["publish_year"] == 2020
    assert book["metadata"]["isbn"] == "9780306406157"
    make_epub(source / "other.epub", title="另一版本", body="版本二")
    scan(ws, source)
    preview = preview_updates(ws.books(), rows)
    assert not preview["updates"] and "多个版本" in preview["issues"][0]["message"]


def test_table_duplicate_rows_no_fuzzy_matching_and_atomic_updates(library):
    from ebook_organizer.table_import import preview_updates, apply_updates
    ws, _, _ = library
    book = ws.books()[0]; bid = book["book_id"]
    preview = preview_updates(ws.books(), [["编号", "作者"], [bid, "甲"], [bid, "乙"]], overwrite=True)
    assert not preview["updates"] and len(preview["issues"]) == 2
    assert not preview_updates(ws.books(), [["书名", "作者"], ["测试图书多了几个字", "甲"]])["updates"]
    with pytest.raises(ValueError):
        ws.edit_many([{"book_id": bid, "changes": {"author": "不应写入"}}, {"book_id": bid, "changes": {"isbn": "bad"}}])
    assert ws.book(bid)["metadata"]["author"] == book["metadata"]["author"]
    preview = preview_updates(ws.books(), [["编号", "出版社"], [bid, "出版社"]])
    ws.edit(bid, {"description": "预检后改动"})
    with pytest.raises(ValueError, match="发生变化"):
        apply_updates(ws, preview)


@pytest.fixture
def pipeline_env(library, monkeypatch):
    from ebook_organizer import pipeline
    from ebook_organizer.connections import SiteClient
    from ebook_organizer.engine import public_record
    ws, source, path = library
    bid = ws.books()[0]["book_id"]
    ws.edit(bid, {"rights_review_status": "confirmed", "copyright_status": "public_domain", "source_reference": "合成测试"})
    calls = {"cover": [], "book": [], "quark": [], "preview": [], "commit": [], "error": False, "choose": False}
    config = {"site_url": "https://books.example.com", "site_id": "testing", "r2_account": "a" * 32,
              "r2_bucket": "covers", "r2_public": "https://img.example.com", "quark_parent": "0"}
    def cover(ws, bid, cfg, credentials):
        calls["cover"].append(bid)
        book = ws.book(bid)
        state = {"state": "verified", "version": book["cover_version"], "account": cfg["r2_account"], "bucket": cfg["r2_bucket"],
                 "url": cfg["r2_public"] + f"/books/{bid}/{book['cover_version']}/cover.webp"}
        ws.save_result(bid, "r2", state)
        return state
    def upload(ws, bid, provider, cfg, credentials, control, connector=None):
        calls["book"].append((bid, provider))
        result = {"state": "shared", "share_url": "https://pan.quark.cn/s/fixture" + bid[3:]}
        ws.save_result(bid, provider, result)
        return result
    def connector(*args):
        calls["quark"].append(True)
        return object()
    previews = {}
    def handler(request):
        if request.url.path.endswith("/info"):
            return httpx.Response(200, json={"site_id": "testing", "categories": [], "max_books": 500})
        data = json.loads(request.content)
        if request.url.path.endswith("/preview"):
            calls["preview"].append(data)
            previews[data["export_id"]] = {book["book_id"]: book for book in data["books"]}
            rows = [{"book_id": b["book_id"], "title": b["title"], "error": None,
                     "action": "choose" if calls["choose"] else "create", "incoming": b,
                     "candidates": [{"id": 42}]} for b in data["books"]]
            return httpx.Response(200, json={"export_id": data["export_id"], "site_id": "testing", "rows": rows})
        calls["commit"].append(data)
        choice = data["choices"][0]; bid = choice["book_id"]
        book = previews[request.url.path.split("/")[-2]][bid]
        item = {"status": "error", "message": "模拟逐本失败"} if calls["error"] else {
            "status": "ok", "revision": book["revision"], "publish_status": "published" if choice.get("publish") else "draft"}
        return httpx.Response(200, json={"site_id": "testing", "items": {bid: item}})
    class Client(SiteClient):
        def __init__(self, ws, cfg, credentials):
            super().__init__(ws, cfg, credentials, client=httpx.Client(transport=httpx.MockTransport(handler)))
            self.owned = True
    class FakeCredentials:
        def get(self, key): return "fixture-only"
    monkeypatch.setattr(pipeline, "upload_cover", cover)
    monkeypatch.setattr(pipeline, "upload_book", upload)
    monkeypatch.setattr(pipeline, "quark_connector", connector)
    monkeypatch.setattr(pipeline, "SiteClient", Client)
    return ws, config, FakeCredentials(), calls


def test_pipeline_uses_fresh_confirmed_fields_and_baidu(pipeline_env):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    bid = ws.books()[0]["book_id"]
    ws.edit(bid, {"title": "Python 编程", "main_category": "", "classification_status": "pending", "rights_review_status": "pending"})
    result = run_full_pipeline(ws, cfg, creds, {"book_ids": [bid], "provider": "baidu", "auto_classify": True,
        "auto_rights": True, "rights_status": "public_domain", "source_reference": "合成测试", "publish": True})
    assert result['classified_auto'] == result['rights_auto'] == 1
    assert result['book_uploaded'] == result['site_synced'] == 0
    assert ws.book(bid)['metadata']['classification_status']=='needs_review'
    ws.edit(bid, {'classification_status':'confirmed'})
    result = run_full_pipeline(ws,cfg,creds,{'book_ids':[bid],'provider':'baidu','publish':True})
    assert result['book_uploaded']==result['site_synced']==1
    assert result["site_published"] == 1 and not calls["quark"]
    assert calls["book"] == [(bid, "baidu")]


@pytest.mark.parametrize("ids", [[], "", ["missing"]])
def test_pipeline_empty_scope_never_processes_all(pipeline_env, ids):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    before = ws.books()
    result = run_full_pipeline(ws, cfg, creds, {"book_ids": ids})
    assert result["total"] == 0 and not calls["book"] and not calls["preview"]
    assert ws.books() == before and ws.setting("last_pipeline_task") is None


def test_pipeline_dry_run_and_exclusions_do_not_write(pipeline_env):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    bid = ws.books()[0]["book_id"]
    ws.edit(bid, {"rights_review_status": "pending"})
    before = ws.books()
    result = run_full_pipeline(ws, cfg, creds, {"dry_run": True, "auto_rights": True, "rights_status": "public_domain", "source_reference": "测试"})
    assert result["_book_would"] == 1 and ws.books() == before
    assert not calls["book"] and not calls["preview"] and not calls["cover"] and ws.setting("last_pipeline_task") is None
    with ws.connect() as db: db.execute("UPDATE books SET excluded=1")
    before = ws.books()
    result = run_full_pipeline(ws, cfg, creds, {"auto_rights": True, "rights_status": "public_domain", "source_reference": "测试"})
    assert result["excluded_skipped"] == 1 and ws.books() == before and not calls["book"]


def test_pipeline_unknown_rights_stays_pending(pipeline_env):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    bid = ws.books()[0]["book_id"]
    ws.edit(bid, {"rights_review_status": "pending", "copyright_status": "", "source_reference": ""})
    result = run_full_pipeline(ws, cfg, creds)
    assert result["needs_rights"] == [bid] and not calls["book"]
    with pytest.raises(ValueError):
        run_full_pipeline(ws, cfg, creds, {"auto_rights": True})


def test_pipeline_receipt_failures_and_ambiguous_matches(pipeline_env):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    calls["error"] = True
    result = run_full_pipeline(ws, cfg, creds)
    assert result["site_failed"] == 1 and result["site_synced"] == 0
    calls["error"] = False; calls["choose"] = True
    count = len(calls["commit"])
    result = run_full_pipeline(ws, cfg, creds)
    assert len(result["needs_binding"]) == 1 and len(calls["commit"]) == count


def test_pipeline_revisions_cover_and_publish_intent_resync(pipeline_env):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    bid = ws.books()[0]["book_id"]
    assert run_full_pipeline(ws, cfg, creds)["site_synced"] == 1
    assert run_full_pipeline(ws, cfg, creds)["site_skipped"] == 1
    ws.edit(bid, {"description": "修订后的简介"})
    assert run_full_pipeline(ws, cfg, creds, {"overwrite": True})["site_synced"] == 1
    assert run_full_pipeline(ws, cfg, creds, {"publish": True})["site_published"] == 1
    with ws.connect() as db:
        db.execute("UPDATE books SET cover_version='new-version', revision=revision+1 WHERE book_id=?", (bid,))
    count = len(calls["cover"])
    assert run_full_pipeline(ws, cfg, creds)["cover_uploaded"] == 1
    assert len(calls["cover"]) == count + 1
    result = run_full_pipeline(ws, cfg, creds, {"force": True})
    assert result["cover_uploaded"] == result["site_synced"] == 1
    assert len(calls["book"]) == 1


def test_pipeline_cancel_preserves_completed_steps_and_job(pipeline_env):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    control = Control()
    def progress(message):
        if message.startswith("分享已生成"):
            control.cancelled.set()
    with pytest.raises(Cancelled):
        run_full_pipeline(ws, cfg, creds, control=control, progress=progress)
    bid = ws.books()[0]["book_id"]
    assert ws.result(bid, "quark")["share_url"]
    assert ws.setting("last_pipeline_task")["book_ids"] == [bid]
    with ws.connect() as db:
        assert db.execute("SELECT status FROM jobs WHERE kind='pipeline'").fetchone()[0] == "cancelled"
    assert run_full_pipeline(ws, cfg, creds, ws.setting("last_pipeline_task"))["site_synced"] == 1
    assert len(calls["book"]) == 1


def test_site_client_rejects_stale_or_deleted_preview(pipeline_env):
    from ebook_organizer import pipeline
    ws, cfg, creds, calls = pipeline_env
    bid = ws.books()[0]["book_id"]
    client = pipeline.SiteClient(ws, cfg, creds)
    try:
        preview = client.preview([bid])
        ws.edit(bid, {"description": "预检后修改"})
        result = client.commit(preview, [{"book_id": bid, "action": "create"}])
        assert result["items"][bid]["status"] == "error" and not calls["commit"]
        preview = client.preview([bid])
        ws.delete_books([bid])
        result = client.commit(preview, [{"book_id": bid, "action": "create"}])
        assert result["items"][bid]["status"] == "error" and not calls["commit"]
    finally:
        client.close()


@pytest.mark.parametrize("key,value", [("r2_account", "b" * 32), ("r2_bucket", "new-covers"), ("r2_public", "https://new.example.com")])
def test_pipeline_rechecks_cover_after_storage_configuration_changes(pipeline_env, key, value):
    from ebook_organizer.pipeline import run_full_pipeline
    ws, cfg, creds, calls = pipeline_env
    run_full_pipeline(ws, cfg, creds)
    cfg[key] = value
    assert run_full_pipeline(ws, cfg, creds)["cover_uploaded"] == 1
    assert len(calls["book"]) == 1


def test_read_table_xlsx_numeric_isbn_and_csv_encoding(library, tmp_path):
    from ebook_organizer.table_import import read_table, preview_updates
    from openpyxl import Workbook
    ws, _, _ = library
    wb = Workbook()
    wb.active.append(["ISBN", "作者"])
    wb.active.append([9780306406157, "表格作者"])
    path = tmp_path / "table.xlsx"
    wb.save(path); wb.close()
    assert len(preview_updates(ws.books(), read_table(path), overwrite=True)["updates"]) == 1
    path = tmp_path / "table.csv"
    path.write_bytes("ISBN,作者\n9780306406157,表格作者\n\n".encode("gb18030"))
    assert len(preview_updates(ws.books(), read_table(path), overwrite=True)["updates"]) == 1


def test_table_only_fills_empty_fields_unless_overwrite_is_explicit(library):
    from ebook_organizer.table_import import preview_updates, apply_updates
    ws, _, _ = library
    book = ws.books()[0]
    rows = [["编号", "作者", "出版社"], [book["book_id"], "新作者", "新出版社"]]
    preview = preview_updates(ws.books(), rows)
    assert preview["updates"][0]["changes"] == {"publisher": "新出版社"}
    apply_updates(ws, preview)
    assert ws.book(book["book_id"])["metadata"]["author"] == book["metadata"]["author"]
    preview = preview_updates(ws.books(), rows, overwrite=True)
    assert preview["updates"][0]["changes"] == {"author": "新作者"}
    control = Control(); control.cancelled.set()
    with pytest.raises(Cancelled):
        apply_updates(ws, preview, control)
    assert ws.book(book["book_id"])["metadata"]["author"] == book["metadata"]["author"]


def test_baidu_new_upload_stages_file_and_retries_only_sharing(library, monkeypatch):
    from ebook_organizer import connections
    ws, _, source = library
    bid = ws.books()[0]["book_id"]
    ws.edit(bid, {"rights_review_status": "confirmed", "copyright_status": "public_domain", "source_reference": "合成测试"})
    checksum = sha256_file(source)
    folders, uploads = [], []
    class Credentials:
        def get(self, key):
            assert key == "baidu_token"
            return "fixture-token"
    class MockHTTP:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, **kwargs):
            folders.append(kwargs["data"]["path"])
            return httpx.Response(200, json={"errno": 0})
    class Connector:
        fail = True
        def _ensure_ok(self, response, operation): assert response["errno"] == 0
        def _upload(self, path):
            assert path != source and sha256_file(path) == checksum
            uploads.append(path)
            return "file-1", "/电子书库/文学小说/中国文学/test.epub"
        async def _share(self, fid):
            assert fid == "file-1"
            if self.fail:
                self.fail = False
                raise ValueError("模拟分享失败")
            return "https://pan.baidu.com/s/fixture", "abcd"
    connector = Connector()
    monkeypatch.setattr(connections.httpx, "Client", MockHTTP)
    with pytest.raises(ValueError, match="模拟分享失败"):
        upload_book(ws, bid, "baidu", {"baidu_root": "电子书库"}, Credentials(), connector=connector)
    result = upload_book(ws, bid, "baidu", {"baidu_root": "电子书库"}, Credentials(), connector=connector)
    assert result["state"] == "shared" and len(uploads) == 1
    assert folders == ["/电子书库", "/电子书库/文学小说", "/电子书库/文学小说/中国文学"]
    assert sha256_file(source) == checksum


def test_desktop_client_with_real_website_contract_draft_update_publish(library, client, db_session, monkeypatch):
    """走真实网站路由和临时数据库，仅将外部网盘检测替换为假结果。"""
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.models import Resource
    from app.core.config import get_settings
    from app.services import organizer_sync
    from test_organizer_sync import auth
    from ebook_organizer.connections import SiteClient
    ws, _, _ = library
    bid = ws.books()[0]["book_id"]
    ws.edit(bid, {"rights_review_status": "confirmed", "copyright_status": "public_domain", "source_reference": "合成测试"})
    ws.save_result(bid, "quark", {"state": "shared", "share_url": "https://pan.quark.cn/s/integrationfixture"})
    secret = auth(db_session)["Authorization"].removeprefix("Bearer ")
    class Credentials:
        def get(self, key): return secret
    def check_link(db, link):
        link.status = "active"; link.is_visible = True
        return SimpleNamespace(result="ok")
    monkeypatch.setattr(organizer_sync, "check_link", check_link)
    config = {"site_url": "http://127.0.0.1", "site_id": get_settings().organizer_site_id}
    desktop = SiteClient(ws, config, Credentials(), client=client)
    preview = desktop.preview([bid])
    result = desktop.commit(preview, [{"book_id": bid, "action": "create"}])
    assert result["items"][bid]["status"] == "ok"
    assert result["items"][bid]["publish_status"] == "draft"
    # 网站现在使用受控分类目录，发布前显式选择已存在的分类。
    ws.edit(bid, {"description": "修订资料在真实网站契约中生效", "main_category": "编程开发", "subcategory": ""})
    preview = desktop.preview([bid])
    assert preview["rows"][0]["action"] == "update"
    result = desktop.commit(preview, [{"book_id": bid, "action": "update", "overwrite": True, "publish": True}])
    assert result["items"][bid]["publish_status"] == "published"
    resource = db_session.scalar(select(Resource))
    assert resource.description == "修订资料在真实网站契约中生效"
    assert ws.books()[0]["metadata"]["title"] in client.get("/books").text
