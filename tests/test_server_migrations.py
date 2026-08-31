from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_isolated_legacy_catalog_upgrade_harness(tmp_path):
    root = Path(__file__).parents[1]
    database = tmp_path / "legacy-catalog.sqlite3"
    env = dict(os.environ, APP_ENV="test", DATABASE_URL=f"sqlite:///{database.as_posix()}",
               LOCAL_STORAGE_ROOT=str(tmp_path / "runtime"), CLOUD_UPLOAD_WORKER_ENABLED="false",
               LINK_CHECK_AUTOMATIC_ENABLED="false", CATALOG_MIGRATION_TEST="synthetic-only")
    for module, args in [("alembic", ["upgrade", "20260831_0007"]), ("scripts.check_catalog_upgrade", [])]:
        result = subprocess.run([sys.executable, "-m", module, *args], cwd=root, env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    # 合成测试也不能误用于有数据的库。
    result = subprocess.run([sys.executable, "-m", "scripts.check_catalog_upgrade"], cwd=root, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    with sqlite3.connect(database) as db:
        assert db.execute("select count(*) from resources").fetchone()[0] == 1


def test_fresh_database_migrations_and_idempotent_bootstrap(tmp_path):
    root = Path(__file__).parents[1]
    database = tmp_path / "fresh.sqlite3"
    env = dict(os.environ, APP_ENV="test", DATABASE_URL=f"sqlite:///{database.as_posix()}",
               LOCAL_STORAGE_ROOT=str(tmp_path / "runtime"), CLOUD_UPLOAD_WORKER_ENABLED="false",
               LINK_CHECK_AUTOMATIC_ENABLED="false", INITIAL_ADMIN_PASSWORD="test-only-password-12345678")
    for module, args in [("alembic", ["upgrade", "head"]), ("scripts.init_production", []),
                         ("alembic", ["upgrade", "head"]), ("scripts.init_production", [])]:
        result = subprocess.run([sys.executable, "-m", module, *args], cwd=root, env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("select version_num from alembic_version").fetchone()[0] == "20260831_0008"
        assert db.execute("select count(*) from admin_users").fetchone()[0] == 1
        assert db.execute("select count(*) from resources").fetchone()[0] == 0
        assert db.execute("select count(*) from providers").fetchone()[0] == 2
        assert db.execute("select count(*) from categories").fetchone()[0] == 8
        for table in ["friend_links", "organizer_tokens", "organizer_identities", "organizer_batches", "category_mappings", "category_redirects"]:
            assert db.execute(f"select count(*) from {table}").fetchone()[0] == 0


def test_catalog_upgrade_preserves_old_books_and_manual_review(tmp_path):
    root = Path(__file__).parents[1]
    database = tmp_path / "existing.sqlite3"
    env = dict(os.environ, APP_ENV="test", DATABASE_URL=f"sqlite:///{database.as_posix()}",
               CLOUD_UPLOAD_WORKER_ENABLED="false", LINK_CHECK_AUTOMATIC_ENABLED="false")
    def migrate(*args):
        result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    migrate("upgrade", "20260831_0004")
    with sqlite3.connect(database) as db:
        for rid in (1, 2):
            db.execute("""INSERT INTO resources (id, resource_code, resource_type, title, normalized_title,
                slug, language, copyright_status, publish_status, view_count, description, created_at, updated_at)
                VALUES (?, ?, 'book', ?, ?, ?, 'zh-CN', 'authorized', 'published', 5, '原始简介', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
                (rid, f"BK-old-{rid}", f"旧书{rid}", f"旧书{rid}", f"old-url-{rid}"))
        db.execute("""INSERT INTO admin_operation_logs (action, entity_type, entity_id, detail, created_at)
            VALUES ('update', 'resource', '1', '{}', CURRENT_TIMESTAMP)""")
        db.execute("""INSERT INTO organizer_identities (book_id, resource_id, epub_sha256, revision, payload_hash, created_at, updated_at)
            VALUES (?, 2, ?, 3, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""", ("BK_"+"a"*32, "a"*64, "b"*64))
        before = db.execute("SELECT id,resource_code,title,slug,copyright_status,publish_status,description FROM resources ORDER BY id").fetchall()
    migrate("upgrade", "head")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT id,resource_code,title,slug,copyright_status,publish_status,description FROM resources ORDER BY id").fetchall() == before
        assert db.execute("SELECT metadata_locked FROM resources ORDER BY id").fetchall() == [(1,), (0,)]
        assert db.execute("SELECT book_id,resource_id,revision FROM organizer_identities").fetchone() == ("BK_"+"a"*32, 2, 3)
        assert db.execute("SELECT count(*) FROM category_redirects").fetchone()[0] == 0
    migrate("downgrade", "20260831_0004")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT id,resource_code,title,slug,copyright_status,publish_status,description FROM resources ORDER BY id").fetchall() == before


def test_sqlite_permanent_id_migration_keeps_rows_and_deleted_id_sequence(tmp_path):
    root = Path(__file__).parents[1]
    database = tmp_path / "book-ids.sqlite3"
    env = dict(os.environ, APP_ENV="test", DATABASE_URL=f"sqlite:///{database.as_posix()}",
               CLOUD_UPLOAD_WORKER_ENABLED="false", LINK_CHECK_AUTOMATIC_ENABLED="false")
    def migrate(*args):
        result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
    def insert_book(db, number):
        return db.execute("""INSERT INTO resources (resource_code, resource_type, title, normalized_title,
            slug, language, copyright_status, publish_status, view_count, created_at, updated_at)
            VALUES (?, 'book', ?, ?, ?, 'zh-CN', 'pending', 'draft', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (f"BK-{number}", f"book {number}", f"book {number}", f"book-{number}")).lastrowid
    migrate("upgrade", "20260831_0005")
    with sqlite3.connect(database) as db:
        first_id = insert_book(db, "old")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='resources'").fetchone()[0]
        assert "AUTOINCREMENT" in ddl.upper()
        assert db.execute("SELECT id,slug FROM resources").fetchall() == [(first_id, "book-old")]
        deleted_id = insert_book(db, "deleted")
        db.execute("DELETE FROM resources WHERE id=?", (deleted_id,))
    # 回退此迁移后再升级也不能把历史高水位降到当前最大 ID。
    migrate("downgrade", "20260831_0005")
    migrate("upgrade", "head")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        new_id = insert_book(db, "new")
        assert new_id > deleted_id > first_id
        assert db.execute("SELECT id,slug FROM resources WHERE id=?", (first_id,)).fetchone() == (first_id, "book-old")
