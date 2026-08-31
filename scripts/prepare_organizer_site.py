"""只适配本地SQLite站点：先一致性备份，再增量迁移，不启动上传任务。"""
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url
from app.core.config import get_settings, PROJECT_ROOT


def main():
    settings = get_settings()
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite":
        raise SystemExit("该脚本只处理本地SQLite。MySQL请先做数据库备份，再单独执行Alembic迁移。")
    database = Path(url.database).resolve()
    if not database.is_file():
        raise SystemExit("现有站点数据库不存在，未创建或改动数据库")
    target = PROJECT_ROOT / "runtime/backups" / ("website_pre_organizer_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".sqlite3")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SystemExit("备份文件已存在，未覆盖")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as backup:
        source.backup(backup)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, check=True)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        report = {"backup": str(target), "migration": db.execute("SELECT version_num FROM alembic_version").fetchone()[0], "resources": db.execute("SELECT count(*) FROM resources").fetchone()[0], "share_links": db.execute("SELECT count(*) FROM channel_share_links").fetchone()[0]}
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
