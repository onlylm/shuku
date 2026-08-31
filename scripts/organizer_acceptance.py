"""离线验收和生成合成样例；不调用云端，不扫描真实书库。"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CLOUD_UPLOAD_WORKER_ENABLED"] = "false"
os.environ["LINK_CHECK_AUTOMATIC_ENABLED"] = "false"

from PySide6.QtWidgets import QApplication
from app.services.organizer_contract import OrganizerPackage
from ebook_organizer.engine import export_snapshot, scan, public_record
from ebook_organizer.safeio import atomic_json
from ebook_organizer.ui import MainWindow
from ebook_organizer.workspace import Workspace, now


def main():
    project = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("fixtures", project / "tests/test_organizer.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    app = QApplication([])
    qa = project / "runtime/organizer-qa"
    qa.mkdir(parents=True, exist_ok=True)
    atomic_json(project / "docs/schemas/organizer-v2.schema.json", OrganizerPackage.model_json_schema())
    with tempfile.TemporaryDirectory(prefix="organizer-acceptance-") as temporary:
        root = Path(temporary)
        fixtures.make_epub(root / "source/文学小说/合成书一.epub", title="合成示例：第一本书")
        fixtures.make_epub(root / "source/科学科普/合成书二.epub", title="合成示例：第二本书", body="独立生成的测试正文")
        workspace = Workspace(root / "workspace")
        scan(workspace, root / "source")
        for book in workspace.books():
            workspace.edit(book["book_id"], {"rights_review_status": "confirmed", "copyright_status": "authorized", "source_reference": "本项目自行生成的测试文件；不包含第三方书籍"})
        workspace.set_setting("connections", {"site_id": "jingye-local"})
        output = export_snapshot(workspace, [b["book_id"] for b in workspace.books()], project / "samples/organizer")
        window = MainWindow(workspace)
        window.show(); window.table.selectRow(0); app.processEvents()
        book = workspace.books()[0]
        window.load_preview({"export_id": "a" * 32, "site_id": "jingye-local", "rows": [{"book_id": book["book_id"], "title": book["metadata"]["title"], "action": "create", "error": None, "candidates": [], "incoming": public_record(workspace, book)}]})
        for index, label in enumerate(["library", "tasks", "settings", "preview"]):
            window.tabs.setCurrentIndex(index); app.processEvents()
            window.grab().save(str(qa / (label + ".png")))
        window.close(); app.processEvents()
        performance_ws = Workspace(root / "performance")
        stamp = now()
        with performance_ws.connect() as db:
            db.executemany("INSERT INTO books(book_id,sha256,metadata,provenance,issues,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", [
                ("BK_" + f"{n:032x}", f"{n:064x}", json.dumps({"title": f"测试性能{n:05d}", "author": "测试作者", "main_category": "文学小说"}, ensure_ascii=False), "{}", "[]", "passed", stamp, stamp)
                for n in range(10000)])
        start = time.perf_counter()
        window = MainWindow(performance_ws)
        window.show(); app.processEvents()
        load_ms = round((time.perf_counter() - start) * 1000)
        assert window.model.rowCount() == 10000
        start = time.perf_counter()
        window.search.setText("测试性能09999"); app.processEvents()
        search_ms = round((time.perf_counter() - start) * 1000)
        assert window.proxy.rowCount() == 1
        window.close(); app.processEvents()
        report = {"generated_at": now(), "sample_export": str(output), "schema": "docs/schemas/organizer-v2.schema.json", "desktop_tabs": 4, "virtual_table_rows": 10000, "load_ms": load_ms, "search_ms": search_ms, "cloud_calls": 0, "real_books_modified": 0}
        atomic_json(qa / "acceptance.json", report)
        print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
