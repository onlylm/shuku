from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="电子书整理工作台")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--scan", type=Path, help="仅执行离线扫描，不启动界面")
    parser.add_argument("--smoke-report", type=Path, help="仅验证打包启动，写入报告并退出，不连接云端")
    args = parser.parse_args()
    from .workspace import Workspace
    default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "EbookOrganizer" / "workspace" if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1] / "runtime" / "organizer"
    workspace = Workspace(args.workspace or default)
    # 桌面进程绝不借用网站数据库配置；仅复用连接器代码。
    os.environ["DATABASE_URL"] = "sqlite:///" + (workspace.root / "connector-state.sqlite3").as_posix()
    os.environ["LOCAL_STORAGE_ROOT"] = str(workspace.root / "connector-runtime")
    os.environ["CLOUD_UPLOAD_WORKER_ENABLED"] = "false"
    os.environ["LINK_CHECK_AUTOMATIC_ENABLED"] = "false"
    os.environ["APP_ENV"] = "development"
    if args.scan:
        from .engine import scan
        print(scan(workspace, args.scan, progress=print))
        return
    from PySide6.QtWidgets import QApplication, QMessageBox
    from .ui import MainWindow
    app = QApplication(sys.argv[:1])
    try:
        window = MainWindow(workspace)
        window.show()
        if args.smoke_report:
            from .safeio import atomic_json
            app.processEvents()
            from . import __version__
            atomic_json(args.smoke_report, {"started": True, "tabs": window.tabs.count(), "version": __version__, "cloud_calls": 0})
            window.close()
            return 0
    except Exception as exc:
        QMessageBox.critical(None, "启动失败", str(exc))
        return 1
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
