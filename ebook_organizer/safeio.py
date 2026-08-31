from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path


class Cancelled(Exception):
    pass


def filesystem_path(path: Path) -> Path:
    """Windows 长路径前缀只用于本地文件访问，不进入公开清单。"""
    if os.name != "nt":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


class Control:
    def __init__(self):
        self.cancelled = threading.Event()
        self.running = threading.Event()
        self.running.set()

    def check(self):
        while not self.running.wait(0.1):
            if self.cancelled.is_set():
                raise Cancelled("任务已取消，可重新执行未完成阶段")
        if self.cancelled.is_set():
            raise Cancelled("任务已取消，可重新执行未完成阶段")


def sha256_file(path: Path, control: Control | None = None) -> str:
    path = filesystem_path(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            if control:
                control.check()
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(value: str, limit: int = 65) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:limit].rstrip(" .")
    if not value:
        return "未命名"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", value, re.I):
        value = "_" + value
    return value


def atomic_bytes(path: Path, content: bytes):
    path = filesystem_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with temporary.open("wb") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    temporary.replace(path)


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def bounded_read(path: Path, limit: int) -> bytes:
    path = filesystem_path(path)
    if is_link(path):
        raise ValueError("不读取符号链接或目录联接附件")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("附件超过处理大小限制")
    return data
