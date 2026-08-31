"""备份完整性检查；文件恢复只允许两个明确的数据卷目录。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile


FILES = ("database.sql.gz", "files.tar.gz", "deploy.env")


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_archive(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            name = PurePosixPath(item.name)
            if name.is_absolute() or ".." in name.parts or "\\" in item.name:
                raise ValueError("备份含非法文件路径")
            if not (item.isfile() or item.isdir()):
                raise ValueError("备份含符号链接、硬链接或特殊文件，请改用人工恢复")
            if not (name.parts[:1] == ("runtime",) or name.parts[:3] == ("app", "static", "covers")):
                raise ValueError("备份包含非数据卷内容，已拒绝恢复")


def seal(directory: Path, revision: str) -> None:
    validate_archive(directory / "files.tar.gz")
    checksums = {name: digest(directory / name) for name in FILES}
    manifest = {"format": 1, "revision": revision, "files": checksums}
    with (directory / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)


def verify(directory: Path) -> None:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or set(manifest.get("files", {})) != set(FILES):
        raise ValueError("备份清单格式不正确，不能恢复")
    for name in FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != manifest["files"][name]:
            raise ValueError(f"备份校验失败：{name}")
    validate_archive(directory / "files.tar.gz")


def restore_files(archive_path: Path) -> None:
    validate_archive(archive_path)
    # 只在服务器容器内使用，路径固定且不得是软链接。
    roots = [Path("/app/runtime"), Path("/app/app/static/covers")]
    for root in roots:
        if not root.is_dir() or root.is_symlink() or root.resolve() != root:
            raise ValueError("数据卷未正确挂载，停止恢复")
    for root in roots:
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall("/app", filter="data")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seal", "verify", "restore-files"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--revision", default="unknown")
    args = parser.parse_args()
    try:
        if args.action == "seal":
            seal(args.path, args.revision)
        elif args.action == "verify":
            verify(args.path)
        else:
            restore_files(args.path)
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        raise SystemExit(f"备份操作失败：{exc}") from None


if __name__ == "__main__":
    main()
