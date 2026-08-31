from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


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
        assert db.execute("select version_num from alembic_version").fetchone()[0] == "20260831_0004"
        assert db.execute("select count(*) from admin_users").fetchone()[0] == 1
        assert db.execute("select count(*) from resources").fetchone()[0] == 0
        assert db.execute("select count(*) from providers").fetchone()[0] == 2
        assert db.execute("select count(*) from categories").fetchone()[0] == 6
        for table in ["friend_links", "organizer_tokens", "organizer_identities", "organizer_batches"]:
            assert db.execute(f"select count(*) from {table}").fetchone()[0] == 0
