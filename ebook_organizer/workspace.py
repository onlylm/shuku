from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from .safeio import filesystem_path


def now():
    return datetime.now(timezone.utc).isoformat()


class Workspace:
    def __init__(self, root: Path | str):
        self.root = filesystem_path(Path(root).resolve())
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "library.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS books(
                    book_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL,
                    metadata TEXT NOT NULL, provenance TEXT NOT NULL, issues TEXT NOT NULL,
                    status TEXT NOT NULL, cover_path TEXT, cover_version TEXT,
                    revision INTEGER NOT NULL DEFAULT 1, locked TEXT NOT NULL DEFAULT '[]',
                    excluded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(
                    path TEXT PRIMARY KEY, root TEXT NOT NULL, book_id TEXT NOT NULL REFERENCES books(book_id),
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, seen_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS edits(
                    id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, previous TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    details TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS results(
                    book_id TEXT NOT NULL, target TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(book_id,target));
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY, book_id TEXT, stage TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_sources_book ON sources(book_id);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES('workspace_id', ?)", (json.dumps(str(uuid.uuid4())),))
            db.execute("INSERT OR IGNORE INTO settings VALUES('schema_version', '1')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            return db.execute("UPDATE jobs SET status='interrupted', updated_at=? WHERE status='running'", (now(),)).rowcount

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        if any(word in key.lower() for word in ("secret", "token", "password", "access_key")):
            raise ValueError("凭据不能写入普通配置")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("metadata", "provenance", "issues", "locked"):
            result[key] = json.loads(result[key])
        return result

    def books(self, search="", issues_only=False):
        with self.connect() as db:
            rows = [self.decode(row) for row in db.execute("SELECT * FROM books ORDER BY created_at,book_id")]
        if search:
            rows = [r for r in rows if search.casefold() in json.dumps(r["metadata"], ensure_ascii=False).casefold() or search in r["book_id"]]
        if issues_only:
            rows = [r for r in rows if r["issues"]]
        return rows

    def book(self, book_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM books WHERE book_id=?", (book_id,)).fetchone())

    def source(self, book_id) -> Path:
        with self.connect() as db:
            rows = db.execute("SELECT path FROM sources WHERE book_id=? ORDER BY seen_at DESC", (book_id,)).fetchall()
        for row in rows:
            if Path(row[0]).is_file():
                return Path(row[0])
        raise ValueError("源文件已移动或缺失，请重新扫描所在目录")

    def edit(self, book_id: str, changes: dict):
        from .epub import isbn_valid
        allowed = {"title", "subtitle", "author", "translator", "publisher", "isbn", "description", "main_category", "subcategory", "copyright_status", "source_reference", "rights_review_status", "classification_status", "language", "publish_year"}
        if set(changes) - allowed:
            raise ValueError("包含不可编辑字段")
        if changes.get("isbn") and not isbn_valid(changes["isbn"]):
            raise ValueError("ISBN 校验不通过，请核对版本或留空")
        with self.connect() as db:
            book = self.decode(db.execute("SELECT * FROM books WHERE book_id=?", (book_id,)).fetchone())
            db.execute("INSERT INTO edits(book_id,previous,created_at) VALUES(?,?,?)", (book_id, json.dumps({"metadata": book["metadata"], "locked": book["locked"]}, ensure_ascii=False), now()))
            book["metadata"].update(changes)
            locked = sorted(set(book["locked"]) | set(changes))
            db.execute("UPDATE books SET metadata=?,locked=?,revision=revision+1,updated_at=? WHERE book_id=?", (json.dumps(book["metadata"], ensure_ascii=False), json.dumps(locked), now(), book_id))

    def delete_book(self, book_id):
        with self.connect() as db:
            db.execute("DELETE FROM results WHERE book_id=?", (book_id,))
            db.execute("DELETE FROM sources WHERE book_id=?", (book_id,))
            db.execute("DELETE FROM edits WHERE book_id=?", (book_id,))
            db.execute("DELETE FROM books WHERE book_id=?", (book_id,))

    def delete_books(self, book_ids):
        """在一个事务里批量删除，避免 700+ 本书逐条提交导致界面卡死。"""
        with self.connect() as db:
            for book_id in book_ids:
                db.execute("DELETE FROM results WHERE book_id=?", (book_id,))
                db.execute("DELETE FROM sources WHERE book_id=?", (book_id,))
                db.execute("DELETE FROM edits WHERE book_id=?", (book_id,))
                db.execute("DELETE FROM books WHERE book_id=?", (book_id,))

    def undo(self, book_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM edits WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,)).fetchone()
            if row:
                old = json.loads(row["previous"])
                db.execute("UPDATE books SET metadata=?,locked=?,revision=revision+1,updated_at=? WHERE book_id=?", (json.dumps(old["metadata"], ensure_ascii=False), json.dumps(old["locked"]), now(), book_id))
                db.execute("DELETE FROM edits WHERE id=?", (row["id"],))

    def result(self, book_id, target):
        with self.connect() as db:
            row = db.execute("SELECT data FROM results WHERE book_id=? AND target=?", (book_id, target)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_result(self, book_id, target, result):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?)", (book_id, target, json.dumps(result, ensure_ascii=False), now()))

    def event(self, stage, message, book_id=None):
        with self.connect() as db:
            db.execute("INSERT INTO events(book_id,stage,message,created_at) VALUES(?,?,?,?)", (book_id, stage, message[:1000], now()))

    def start_job(self, kind, details):
        job_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,'running',?,?,?)", (job_id, kind, json.dumps(details, ensure_ascii=False), now(), now()))
        return job_id

    def finish_job(self, job_id, status, details):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,details=?,updated_at=? WHERE id=?", (status, json.dumps(details, ensure_ascii=False), now(), job_id))

    def backup(self, destination: Path):
        if destination.exists():
            raise ValueError("备份目标已存在，请使用新文件名")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db, closing(sqlite3.connect(destination)) as target:
            db.backup(target)
