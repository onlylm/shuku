"""公开发布保护：仅检查路径规则，不读取个人资料或调用云端服务。"""
import pytest

from scripts.check_public_release import FORBIDDEN_PATHS, is_private_path


@pytest.mark.parametrize("path", sorted(FORBIDDEN_PATHS) + [
    ".private/notes.md",
    "docs/internal/handoff.md",
    "prototype/index.html",
    "prototype/assets/app.js",
    "runtime/log.txt",
    "backups/snapshot/deploy.env",
    "deploy/.env",
    "deploy/control/status/job-test.json",
    "deploy/domains/aliases.caddy",
    ".env.production",
    "local.db",
    "data/local.sqlite3-wal",
    "book.EPUB",
    "samples/organizer/books.json",
    "dist/Ebook/Ebook.exe",
    "app/private.key",
])
def test_private_paths_are_blocked(path):
    assert is_private_path(path)


@pytest.mark.parametrize("path", [
    "README.md",
    "docs/README.md",
    "docs/部署说明.md",
    "docs/已知问题与使用限制.md",
    "docs/本地整理软件使用说明.md",
    "docs/API-DESIGN.md",
    "docs/schemas/organizer-v2.schema.json",
    "deploy/.env.example",
    ".env.example",
    "deploy/compose.yml",
    "deploy.sh",
    "app/main.py",
    "ebook_organizer/ui.py",
    "requirements-organizer-win.lock",
    "tests/test_organizer.py",
])
def test_public_source_and_help_are_allowed(path):
    assert not is_private_path(path)
