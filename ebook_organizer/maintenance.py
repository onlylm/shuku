from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath

from .safeio import atomic_bytes, filesystem_path, sha256_file, is_link


def backup_workspace(workspace, destination: Path):
    destination = filesystem_path(destination.resolve())
    if destination.exists():
        raise ValueError("目标备份已存在，请选择新文件名")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    with tempfile.TemporaryDirectory(prefix="ebook-backup-") as temporary:
        database = Path(temporary) / "library.sqlite3"
        workspace.backup(database)
        files = [(database, "library.sqlite3")]
        for folder in ("covers", "originals"):
            base = workspace.root / folder
            if base.exists():
                files += [(path, path.relative_to(workspace.root).as_posix()) for path in base.rglob("*") if path.is_file() and not is_link(path)]
        manifest = []
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED) as archive:
            for path, name in files:
                checksum = sha256_file(path)
                archive.write(path, name)
                manifest.append({"path": name, "sha256": checksum, "size": path.stat().st_size})
            archive.writestr("manifest.json", json.dumps({"version": 1, "files": manifest}).encode())
        part.replace(destination)
    return destination


def restore_workspace(archive_path: Path, destination: Path):
    destination = filesystem_path(destination.resolve())
    if destination.exists():
        raise ValueError("恢复目标必须是尚不存在的新目录，不覆盖当前工作区")
    staging = destination.with_name(destination.name + ".restore-" + uuid.uuid4().hex[:8])
    staging.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo("manifest.json")
        if info.file_size > 8 * 1024**2 or len(archive.infolist()) > 100_000:
            raise ValueError("备份清单或文件数量超过限制")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("version") != 1:
            raise ValueError("不支持的备份版本")
        seen, total = set(), 0
        for entry in manifest["files"]:
            name = entry["path"]
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or name in seen or not (name == "library.sqlite3" or path.parts[0] in {"covers", "originals"}):
                raise ValueError("备份包含不安全或重复的路径")
            seen.add(name)
            info = archive.getinfo(name)
            total += info.file_size
            if info.file_size > 512 * 1024**2 or total > 4 * 1024**3:
                raise ValueError("备份超过恢复大小限制")
            data = archive.read(name)
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("备份内容校验失败")
            atomic_bytes(staging / name, data)
        if "library.sqlite3" not in seen:
            raise ValueError("备份缺少身份数据库")
    with closing(sqlite3.connect(staging / "library.sqlite3")) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库校验失败")
    staging.rename(destination)
    return destination
