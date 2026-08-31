"""网页只写受限请求，不运行 Git、Shell，也不持有 Docker 控制权。"""
from __future__ import annotations

import json
import os
import secrets
import time

from app.core.config import get_settings
from scripts.maintenance_protocol import PROTOCOL, validate_job


def control_status() -> dict:
    root = get_settings().maintenance_control_root
    result = {"enabled": False, "jobs": [], "message": "尚未连接服务器维护服务；本地可编辑网站资料、账号并检查版本。"}
    if not root:
        return result
    try:
        heartbeat = json.loads((root / "status" / "heartbeat.json").read_text(encoding="utf-8"))
        result["enabled"] = heartbeat.get("protocol") == PROTOCOL and 0 <= time.time() - heartbeat["time"] < 90
        for path in sorted((root / "status").glob("job-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            result["jobs"].append(json.loads(path.read_text(encoding="utf-8")))
        result["busy"] = (root / "requests" / "pending.json").exists() or (root / "status" / "active.json").exists()
        if result["enabled"]:
            result["message"] = "维护服务已连接" + ("，任务处理中，请勿重复提交" if result["busy"] else "")
        else:
            result["message"] = "维护服务暂未响应；不会执行新的域名切换或升级。"
    except (OSError, ValueError, TypeError, KeyError):
        result["message"] = "维护服务尚未就绪，请完成 v2 的服务器部署。"
    return result


def enqueue(kind: str, payload: dict) -> str:
    state = control_status()
    if not state["enabled"]:
        raise ValueError(state["message"])
    if state.get("busy"):
        raise ValueError("已有维护任务，请等待完成后再操作")
    job = {"id": secrets.token_hex(16), "protocol": PROTOCOL, "kind": kind, "payload": payload, "created_at": time.time()}
    validate_job(job)
    path = get_settings().maintenance_control_root / "requests" / "pending.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("已有维护任务，请勿重复提交") from None
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(job, stream, ensure_ascii=False)
    return job["id"]
