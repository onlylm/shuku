"""网页与 Linux 维护服务共用的受限协议，仅使用标准库。"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from scripts.server_config import hostname

REPOSITORY = "onlylm/shuku"
REPOSITORY_URL = "https://github.com/" + REPOSITORY + ".git"
API_URL = "https://api.github.com/repos/" + REPOSITORY
PROTOCOL = 1


def current_version() -> str:
    return (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()


def version_key(value: str) -> tuple:
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-dev)?", value)
    if not match:
        raise ValueError("版本号格式不支持")
    return (*map(int, match.group(1, 2, 3)), 0 if match.group(4) else 1)


def github_json(path: str) -> dict:
    request = urllib.request.Request(API_URL + path, headers={"Accept": "application/vnd.github+json",
        "User-Agent": "Shuku-Version-Check", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("版本信息超过允许大小")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("版本服务返回格式异常")
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("暂未找到正式发布版本") from None
        if exc.code in {403, 429}:
            raise ValueError("版本检查暂时受限，请稍后重试") from None
        raise ValueError("版本服务暂时不可用，现有网站不受影响") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("无法连接版本服务，请稍后重试，现有网站不受影响") from exc


def release_info(tag: str | None = None) -> dict:
    if tag is not None and not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("仅允许正式版本标签，例如 v2.0.0")
    release = github_json("/releases/tags/" + tag if tag else "/releases/latest")
    release_tag = release.get("tag_name", "")
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v\d+\.\d+\.\d+", release_tag):
        raise ValueError("该版本不是受支持的正式发布版本")
    version_key(release_tag)
    if tag and tag != release_tag:
        raise ValueError("版本标签不一致")
    sha = github_json("/commits/" + release_tag).get("sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("版本校验信息不完整")
    return {"tag": release_tag, "sha": sha, "notes": str(release.get("body") or "暂无更新说明")[:12000],
        "url": f"https://github.com/{REPOSITORY}/releases/tag/{release_tag}",
        "checked_at": time.time(), "available": version_key(release_tag) > version_key(current_version())}


def validate_job(job: dict) -> dict:
    if not isinstance(job, dict) or set(job) != {"id", "protocol", "kind", "payload", "created_at"}:
        raise ValueError("维护请求格式不正确")
    if job["protocol"] != PROTOCOL or not re.fullmatch(r"[a-f0-9]{32}", str(job["id"])):
        raise ValueError("维护协议或请求编号无效")
    if not isinstance(job["created_at"], (int, float)) or not -30 <= time.time() - job["created_at"] <= 900:
        raise ValueError("维护请求已过期，请重新确认")
    payload = job["payload"]
    if not isinstance(payload, dict):
        raise ValueError("维护参数格式错误")
    if job["kind"] == "backup":
        if payload:
            raise ValueError("备份不接受自定义路径或命令")
    elif job["kind"] == "domains":
        if set(payload) != {"primary", "aliases", "previous_primary"} or not isinstance(payload["aliases"], list) or len(payload["aliases"]) > 20:
            raise ValueError("域名配置格式错误")
        for value in [payload["primary"], payload["previous_primary"], *payload["aliases"]]:
            if not isinstance(value, str) or hostname(value) != value:
                raise ValueError("域名格式错误")
    elif job["kind"] == "update":
        if set(payload) != {"tag", "sha"} or not re.fullmatch(r"v\d+\.\d+\.\d+", str(payload["tag"])) or not re.fullmatch(r"[a-f0-9]{40}", str(payload["sha"])):
            raise ValueError("升级只能使用已确认的正式版本和完整校验编号")
        version_key(payload["tag"])
    else:
        raise ValueError("不支持的维护操作")
    return job
