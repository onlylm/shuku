"""使用合成文件验证桌面、导出和备份，不接触真实书库及云端。"""
from pathlib import Path
import importlib.util
import json
import os
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CLOUD_UPLOAD_WORKER_ENABLED"] = "false"
from PySide6.QtWidgets import QApplication
from ebook_organizer.engine import scan, export_snapshot
from ebook_organizer.ui import MainWindow
from ebook_organizer.workspace import Workspace


def main():
    project = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("organizer_fixtures", project / "tests/test_organizer.py")
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix="organizer-qa-") as temporary:
        root = Path(temporary)
        fixtures.make_epub(root / "source/文学小说/中国文学/测试一.epub", title="合成测试图书一")
        fixtures.make_epub(root / "source/历史文化/中国历史/测试二.epub", title="合成测试图书二", body="第二本正文", cover=False)
        workspace = Workspace(root / "workspace")
        scan(workspace, root / "source")
        window = MainWindow(workspace)
        window.show(); app.processEvents()
        assert window.model.rowCount() == 2
        window.table.selectRow(0); app.processEvents()
        assert window.current_id
        target = project / "runtime/organizer-qa"
        target.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(target / "desktop.png"))
        exported = export_snapshot(workspace, [b["book_id"] for b in workspace.books()], root / "exports")
        assert (exported / "数据/books.json").exists()
        workspace.backup(root / "backup.sqlite3")
        window.close(); app.processEvents()
        print(json.dumps({"ui": "passed", "books": 2, "export": "passed", "backup": "passed", "screenshot": str(target / "desktop.png")}, ensure_ascii=True))


if __name__ == "__main__":
    main()
