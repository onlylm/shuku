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
