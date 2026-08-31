from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import SessionLocal
from app.models import BackgroundTask, Provider, Resource, ResourceChannel, ResourceFile
from app.models.base import utcnow
from app.services.links import DuplicateLinkError, add_or_replace_link, check_link
from app.services.resources import create_resource
from app.services.text import normalize_title


SUPPORTED_BOOK_EXTENSIONS = {".epub", ".mobi", ".azw3", ".pdf", ".txt", ".docx"}
BAIDU_MCP_URL = "https://mcp-pan.baidu.com/sse"
QUARK_DOWNLOAD_HOSTS = {"open-api-drive.quark.cn", "pdds.quark.cn"}


class CloudConnectorError(RuntimeError):
    pass


class ConnectorAuthRequired(CloudConnectorError):
    pass


@dataclass(slots=True)
class ConnectorStatus:
    code: str
    name: str
    installed: bool
    configured: bool
    message: str
    status_label: str = "已就绪"
    authorization_state: str | None = None
    account_name: str | None = None


@dataclass(slots=True)
class UploadOutcome:
    provider_code: str
    provider_file_id: str
    share_url: str
    extract_code: str | None
    remote_path: str | None = None


@dataclass(slots=True)
class LocalFileCandidate:
    path: str
    name: str
    size: int
    file_format: str
    resource_id: int | None
    resource_title: str | None


@dataclass(slots=True)
class QueueResult:
    queued: int = 0
    skipped: int = 0
    created_resources: int = 0


@dataclass(slots=True)
class UploadProgress:
    """同一批入队任务的进度概览，用于后台「共 1000 个、已完成 5 个」这类展示。"""

    total: int = 0
    completed: int = 0
    running: int = 0
    pending: int = 0
    failed: int = 0
    needs_auth: int = 0
    cancelled: int = 0
    percent: int = 0
    active: bool = False
    batch_key: str = ""

    @property
    def finished(self) -> int:
        return self.completed

    @property
    def scope_label(self) -> str:
        return "本轮进度" if self.active else "上一轮进度"


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_source_path(raw_path: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    if not raw_path.strip():
        raise ValueError("请填写文件或文件夹路径")
    path = Path(raw_path.strip()).expanduser().resolve()
    if not path.exists():
        raise ValueError("本地文件或文件夹不存在")
    roots = settings.upload_source_roots()
    if roots and not any(_path_is_within(path, root) for root in roots):
        raise ValueError("该路径不在允许上传的本地目录中")
    if settings.app_env == "production" and not roots:
        raise ValueError("服务器尚未配置允许上传的本地目录")
    return path


def _resource_title_candidates(path: Path) -> list[str]:
    stem = re.sub(r"\s*\(\d+\)$", "", path.stem).strip()
    parent = re.sub(r"\s*\(\d+\)$", "", path.parent.name).strip()
    values = [stem]
    for separator in (" - ", "_", "—"):
        if separator in stem:
            values.append(stem.split(separator, 1)[0].strip())
    if parent and parent not in values:
        values.append(parent)
    return [value for value in values if value]


def match_resource_for_file(db: Session, path: Path) -> Resource | None:
    for title in _resource_title_candidates(path):
        resource = db.scalar(select(Resource).where(Resource.normalized_title == normalize_title(title)))
        if resource:
            return resource
    return None


def scan_local_files(
    db: Session,
    raw_path: str,
    settings: Settings | None = None,
    limit: int | None = None,
) -> list[LocalFileCandidate]:
    settings = settings or get_settings()
    scan_limit = max(1, min(limit or settings.cloud_upload_max_scan_files, settings.cloud_upload_max_scan_files))
    source = validate_source_path(raw_path, settings)
    paths = [source] if source.is_file() else source.rglob("*")
    candidates: list[LocalFileCandidate] = []
    for path in paths:
        if not path.is_file() or path.suffix.casefold() not in SUPPORTED_BOOK_EXTENSIONS:
            continue
        resolved = path.resolve()
        resource = match_resource_for_file(db, resolved)
        candidates.append(
            LocalFileCandidate(
                path=str(resolved),
                name=resolved.name,
                size=resolved.stat().st_size,
                file_format=resolved.suffix.lstrip(".").upper(),
                resource_id=resource.id if resource else None,
                resource_title=resource.title if resource else None,
            )
        )
        if len(candidates) >= scan_limit:
            break
    return candidates


def _title_author_from_file(path: Path) -> tuple[str, str | None]:
    title = _resource_title_candidates(path)[0]
    author = None
    if " - " in title:
        title, author = (item.strip() for item in title.split(" - ", 1))
    return title or path.stem, author or None


def _source_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def queue_upload_tasks(
    db: Session,
    selected_paths: list[str],
    provider_codes: list[str],
    *,
    auto_create_resource: bool,
    publish_after_upload: bool,
    admin_id: int | None = None,
    settings: Settings | None = None,
) -> QueueResult:
    settings = settings or get_settings()
    providers = set(db.scalars(select(Provider.code).where(Provider.code.in_(provider_codes))))
    invalid_providers = set(provider_codes) - providers
    if invalid_providers:
        raise ValueError("包含尚未启用的网盘渠道")
    if not provider_codes:
        raise ValueError("请至少选择一个网盘")

    batch_key = f"{secrets.token_hex(5)}"
    result = QueueResult()
    for raw_path in dict.fromkeys(selected_paths):
        path = validate_source_path(raw_path, settings)
        if not path.is_file() or path.suffix.casefold() not in SUPPORTED_BOOK_EXTENSIONS:
            result.skipped += len(provider_codes)
            continue
        resource = match_resource_for_file(db, path)
        if not resource and auto_create_resource:
            title, author = _title_author_from_file(path)
            resource = create_resource(
                db,
                {
                    "title": title,
                    "author": author,
                    "formats": path.suffix.lstrip(".").upper(),
                    "copyright_status": "authorized",
                    "publish_status": "draft",
                },
            )
            result.created_resources += 1
        if not resource:
            result.skipped += len(provider_codes)
            continue

        signature = _source_signature(path)
        for provider_code in dict.fromkeys(provider_codes):
            # 去重：同一网盘、同一资源、同一路径、同一文件签名。按数据库查询，不限于最近 N 条，支持几十万书规模。
            existing = db.scalar(
                select(func.count())
                .select_from(BackgroundTask)
                .where(
                    BackgroundTask.task_type == "cloud_upload",
                    BackgroundTask.payload["provider_code"].as_string() == provider_code,
                    BackgroundTask.payload["resource_id"].as_integer() == resource.id,
                    BackgroundTask.payload["local_path"].as_string() == str(path),
                    BackgroundTask.payload["source_signature"].as_string() == signature,
                )
            )
            if existing:
                result.skipped += 1
                continue
            task = BackgroundTask(
                task_type="cloud_upload",
                status="pending",
                payload={
                    "provider_code": provider_code,
                    "resource_id": resource.id,
                    "resource_title": resource.title,
                    "local_path": str(path),
                    "file_name": path.name,
                    "source_signature": signature,
                    "publish_after_upload": publish_after_upload,
                    "created_by_id": admin_id,
                    "batch_key": batch_key,
                },
            )
            db.add(task)
            result.queued += 1
    db.flush()
    return result


def _safe_error_text(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:1000]


def _json_result_events(output: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _result_event(output: str) -> dict[str, Any]:
    events = _json_result_events(output)
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        raise CloudConnectorError("网盘连接器没有返回可识别的结果")
    result = results[-1]
    if int(result.get("code") or 0) != 0:
        message = _safe_error_text(result.get("msg") or "网盘操作失败")
        if any(word in message.casefold() for word in ("未授权", "未登录", "认证", "token", "auth")):
            raise ConnectorAuthRequired(message)
        raise CloudConnectorError(message)
    return result


def _node_executable(settings: Settings) -> Path | None:
    if settings.cloud_node_executable and settings.cloud_node_executable.exists():
        return settings.cloud_node_executable
    found = shutil.which("node")
    if found:
        return Path(found)
    bundled = (
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    )
    return bundled if bundled.exists() else None


def _quark_connector_base(settings: Settings) -> Path:
    return settings.local_storage_root / "cloud-connectors"


def find_quark_cli(settings: Settings | None = None) -> Path | None:
    settings = settings or get_settings()
    if settings.quark_cli_path and settings.quark_cli_path.exists():
        return settings.quark_cli_path
    base = _quark_connector_base(settings)
    pointer = base / "quark-current.json"
    if pointer.exists():
        try:
            path = Path(json.loads(pointer.read_text(encoding="utf-8"))["cli_path"])
            if path.exists():
                return path
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
    matches = sorted(base.glob("quark-*/**/scripts/quark-drive.cjs"), reverse=True) if base.exists() else []
    return matches[0] if matches else None


def _detect_quark_agent(cli: Path, node: Path) -> str | None:
    """使用官方命令确认当前服务是否运行在受支持的 Agent 环境中。"""
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        process = subprocess.run(
            [str(node), str(cli), "resolve-agent"],
            cwd=cli.parent.parent,
            env={**os.environ, "NO_COLOR": "1"},
            capture_output=True,
            timeout=10,
            check=False,
            startupinfo=startupinfo,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if process.returncode != 0:
        return None
    output = process.stdout.decode("utf-8", errors="replace")
    match = re.search(r"QK_AGENT_ID=([a-z0-9_-]+)", output, re.IGNORECASE)
    return match.group(1).casefold() if match else None


def _stored_authorization(db: Session | None, provider_code: str) -> dict[str, Any]:
    if db is None:
        return {}
    provider = db.scalar(select(Provider).where(Provider.code == provider_code))
    if not provider:
        return {}
    authorization = (provider.capabilities or {}).get("authorization")
    return authorization if isinstance(authorization, dict) else {}


def _record_authorization(
    db: Session,
    provider_code: str,
    state: str,
    message: str,
    account_name: str | None = None,
) -> None:
    provider = db.scalar(select(Provider).where(Provider.code == provider_code))
    if not provider:
        return
    capabilities = dict(provider.capabilities or {})
    capabilities["authorization"] = {
        "state": state,
        "message": _safe_error_text(message),
        "account_name": account_name,
        "checked_at": utcnow().isoformat(),
    }
    provider.capabilities = capabilities


def quark_connector_status(
    settings: Settings | None = None,
    db: Session | None = None,
) -> ConnectorStatus:
    settings = settings or get_settings()
    cli = find_quark_cli(settings)
    node = _node_executable(settings)
    if not cli:
        return ConnectorStatus(
            "quark", "夸克网盘", False, False, "尚未安装官方连接器", "未安装", "unavailable"
        )
    if not node:
        return ConnectorStatus(
            "quark", "夸克网盘", True, False, "缺少 Node.js 运行环境", "环境不可用", "unavailable"
        )
    if not _detect_quark_agent(cli, node):
        return ConnectorStatus(
            "quark",
            "夸克网盘",
            True,
            False,
            "当前网站进程不在夸克官方支持的 Agent 环境中，请从 Codex 重新启动本地服务",
            "环境不可用",
            "unavailable",
        )

    authorization = _stored_authorization(db, "quark")
    state = str(authorization.get("state") or "required")
    account_name = str(authorization.get("account_name") or "").strip() or None
    if state == "authorized":
        message = f"账号 {account_name} 已通过官方连接器授权" if account_name else "夸克账号已通过官方连接器授权"
        return ConnectorStatus("quark", "夸克网盘", True, True, message, "已授权", state, account_name)
    if state == "checking":
        return ConnectorStatus(
            "quark", "夸克网盘", True, True, "正在确认账号授权，请在浏览器中完成操作", "授权处理中", state
        )
    message = str(authorization.get("message") or "尚未完成夸克账号授权")
    return ConnectorStatus("quark", "夸克网盘", True, True, message, "需要授权", "required")


def _download_bytes(url: str, max_bytes: int = 100 * 1024 * 1024) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in QUARK_DOWNLOAD_HOSTS:
        raise CloudConnectorError("官方连接器下载地址不在允许的夸克域名内")
    request = urllib.request.Request(url, headers={"User-Agent": "JingyeCloudUploader/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - host allowlist above
            data = response.read(max_bytes + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise CloudConnectorError("无法连接夸克官方连接器服务，请稍后重试") from exc
    if len(data) > max_bytes:
        raise CloudConnectorError("官方连接器安装包过大，已停止下载")
    return data


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if not _path_is_within(target, destination.resolve()):
                raise CloudConnectorError("连接器安装包包含不安全路径")
        bundle.extractall(destination)


def install_quark_connector(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    separator = "&" if "?" in settings.quark_skill_config_url else "?"
    request_id = f"{int(time.time() * 1000)}{secrets.token_hex(3)}"
    config_url = f"{settings.quark_skill_config_url}{separator}req_id={request_id}"
    config_bytes = _download_bytes(config_url, max_bytes=2 * 1024 * 1024)
    try:
        response = json.loads(config_bytes)
    except json.JSONDecodeError as exc:
        raise CloudConnectorError("无法读取夸克官方连接器配置") from exc
    data = response.get("data") or {}
    config = data.get("config") or response.get("config") or data
    download_url = str(config.get("qkPan") or "")
    version = re.sub(r"[^0-9A-Za-z._-]", "-", str(config.get("qkPanVersion") or "current"))
    if not download_url:
        raise CloudConnectorError("夸克官方接口未返回连接器下载地址")
    package = _download_bytes(download_url)
    base = _quark_connector_base(settings)
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quark-{version}"
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix="quark-connector-", dir=base) as temp_dir:
            temp = Path(temp_dir)
            archive = temp / "connector.zip"
            archive.write_bytes(package)
            extracted = temp / "extracted"
            extracted.mkdir()
            _safe_extract_zip(archive, extracted)
            scripts = list(extracted.glob("**/scripts/quark-drive.cjs"))
            if not scripts:
                raise CloudConnectorError("夸克官方安装包中未找到连接器程序")
            package_root = scripts[0].parent.parent
            shutil.copytree(package_root, target)
    cli = target / "scripts" / "quark-drive.cjs"
    if not cli.exists():
        raise CloudConnectorError("夸克连接器安装不完整")
    (base / "quark-current.json").write_text(
        json.dumps({"version": version, "cli_path": str(cli)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return cli


class QuarkCloudConnector:
    code = "quark"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.cli = find_quark_cli(self.settings)
        self.node = _node_executable(self.settings)
        if not self.cli or not self.node:
            raise CloudConnectorError("夸克官方连接器尚未安装或缺少 Node.js")

    def _run(self, arguments: list[str], timeout: int | None = None) -> dict[str, Any]:
        startupinfo = None
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            connector_env = {**os.environ, "NO_COLOR": "1"}
            # 官方连接器会识别这些 Codex 环境变量。保留一个明确标记，避免子进程链丢失身份。
            if any(
                connector_env.get(name)
                for name in ("CODEX_THREAD_ID", "CODEX_CI", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE")
            ):
                connector_env.setdefault("CODEX_ENV", "1")
            process = subprocess.run(
                [str(self.node), str(self.cli), *arguments],
                cwd=self.cli.parent.parent,
                env=connector_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.settings.cloud_upload_timeout_seconds,
                check=False,
                startupinfo=startupinfo,
            )
        except subprocess.TimeoutExpired as exc:
            raise CloudConnectorError("夸克网盘操作超时，可稍后重试，已上传部分会保留断点") from exc
        output = "\n".join(part for part in (process.stdout, process.stderr) if part)
        return _result_event(output)

    def login(self) -> dict[str, Any]:
        return self._run(["login"], timeout=120)

    def get_user_info(self) -> dict[str, Any]:
        return self._run(["get-user-info"], timeout=30)

    def authorize(self) -> dict[str, Any]:
        """校验现有令牌；令牌失效时解除旧授权并重新走官方授权流程。"""
        try:
            return self.get_user_info()
        except ConnectorAuthRequired:
            try:
                self._run(["unauthorize"], timeout=30)
            except CloudConnectorError:
                pass
            self.login()
            return self.get_user_info()

    def upload_and_share(self, local_path: Path, resource_title: str) -> UploadOutcome:
        upload = self._run(["upload", str(local_path)])
        data = upload.get("data") or {}
        fids = [str(value) for value in (data.get("fids") or []) if value]
        if not fids:
            raise CloudConnectorError("夸克上传完成，但未返回文件编号")
        shared = self._run(
            ["share", *fids, "--title", resource_title, "--url-type", "1", "--expired-type", "1"],
            timeout=120,
        )
        share_data = shared.get("data") or {}
        share_url = str(share_data.get("share_url") or "").strip()
        if not share_url:
            raise CloudConnectorError("夸克已上传文件，但未能生成分享链接")
        return UploadOutcome(
            provider_code=self.code,
            provider_file_id=",".join(fids),
            share_url=share_url,
            extract_code=str(share_data.get("passcode") or "").strip() or None,
            remote_path=str(data.get("fullPath") or "").strip() or None,
        )


def baidu_connector_status(settings: Settings | None = None) -> ConnectorStatus:
    settings = settings or get_settings()
    if settings.baidu_netdisk_access_token:
        return ConnectorStatus("baidu", "百度网盘", True, True, "授权令牌已配置", "已授权", "authorized")
    return ConnectorStatus("baidu", "百度网盘", True, False, "等待开放平台授权", "需要授权", "required")


class BaiduCloudConnector:
    code = "baidu"
    chunk_size = 4 * 1024 * 1024

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.token = (self.settings.baidu_netdisk_access_token or "").strip()
        if not self.token:
            raise ConnectorAuthRequired("百度网盘尚未配置授权令牌")

    def _chunk_hashes(self, path: Path) -> list[str]:
        import hashlib

        hashes: list[str] = []
        with path.open("rb") as handle:
            while chunk := handle.read(self.chunk_size):
                hashes.append(hashlib.md5(chunk).hexdigest())  # noqa: S324 - Baidu protocol requires MD5
        return hashes

    @staticmethod
    def _ensure_ok(data: dict[str, Any], operation: str) -> dict[str, Any]:
        errno = int(data.get("errno") or 0)
        if errno in {-6, 111, -10}:
            raise ConnectorAuthRequired(f"百度网盘授权已失效（{errno}）")
        if errno != 0:
            raise CloudConnectorError(f"百度网盘{operation}失败（{errno}）")
        return data

    def _upload(self, local_path: Path) -> tuple[str, str]:
        remote_dir = "/" + self.settings.baidu_netdisk_remote_dir.strip("/")
        remote_path = f"{remote_dir}/{local_path.name}"
        size = local_path.stat().st_size
        hashes = self._chunk_hashes(local_path)
        block_list = json.dumps(hashes)
        timeout = httpx.Timeout(60, read=300, write=300)
        with httpx.Client(timeout=timeout) as client:
            precreate = self._ensure_ok(
                client.post(
                    "https://pan.baidu.com/rest/2.0/xpan/file",
                    params={"method": "precreate", "access_token": self.token},
                    data={
                        "path": remote_path,
                        "size": str(size),
                        "isdir": "0",
                        "autoinit": "1",
                        "rtype": "3",
                        "block_list": block_list,
                    },
                ).json(),
                "预创建文件",
            )
            upload_id = str(precreate.get("uploadid") or "")
            if not upload_id:
                raise CloudConnectorError("百度网盘未返回上传任务编号")
            missing_parts = precreate.get("block_list")
            part_indexes = [int(value) for value in missing_parts] if isinstance(missing_parts, list) else list(range(len(hashes)))
            with local_path.open("rb") as handle:
                for index in range(len(hashes)):
                    chunk = handle.read(self.chunk_size)
                    if index not in part_indexes:
                        continue
                    response = client.post(
                        "https://d.pcs.baidu.com/rest/2.0/pcs/superfile2",
                        params={
                            "method": "upload",
                            "type": "tmpfile",
                            "access_token": self.token,
                            "path": remote_path,
                            "uploadid": upload_id,
                            "partseq": str(index),
                        },
                        files={"file": (local_path.name, chunk, "application/octet-stream")},
                    )
                    part = response.json()
                    if not part.get("md5"):
                        self._ensure_ok(part, f"上传第 {index + 1} 个分片")
                        raise CloudConnectorError(f"百度网盘上传第 {index + 1} 个分片失败")
            created = self._ensure_ok(
                client.post(
                    "https://pan.baidu.com/rest/2.0/xpan/file",
                    params={"method": "create", "access_token": self.token},
                    data={
                        "path": remote_path,
                        "size": str(size),
                        "isdir": "0",
                        "rtype": "3",
                        "uploadid": upload_id,
                        "block_list": block_list,
                    },
                ).json(),
                "合并文件",
            )
        file_id = str(created.get("fs_id") or "")
        if not file_id:
            raise CloudConnectorError("百度网盘上传完成，但未返回文件编号")
        return file_id, remote_path

    async def _share(self, file_id: str) -> tuple[str, str | None]:
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as exc:
            raise CloudConnectorError("缺少百度网盘官方连接组件，请重新安装网站依赖") from exc

        url = f"{BAIDU_MCP_URL}?access_token={self.token}"
        async with sse_client(url) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool(
                    "file_sharelink_set",
                    {
                        "fsid_list": json.dumps([file_id]),
                        "period": 0,
                        "pwd": "jy88",
                    },
                )
        if getattr(result, "isError", False):
            raise CloudConnectorError("百度网盘已上传文件，但创建分享链接失败")
        values: list[dict[str, Any]] = []
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            values.append(structured)
        texts: list[str] = []
        for block in getattr(result, "content", []):
            text = getattr(block, "text", None)
            if not text:
                continue
            texts.append(text)
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
        for value in values:
            data = value.get("data") if isinstance(value.get("data"), dict) else value
            link = str(data.get("short_url") or data.get("link") or "").strip()
            if link:
                return link, str(data.get("pwd") or "").strip() or "jy88"
        match = re.search(r"https://pan\.baidu\.com/\S+", "\n".join(texts))
        if match:
            return match.group(0).rstrip(".,，。"), "jy88"
        raise CloudConnectorError("百度网盘已上传文件，但官方接口未返回分享链接")

    def upload_and_share(self, local_path: Path, resource_title: str) -> UploadOutcome:
        file_id, remote_path = self._upload(local_path)
        share_url, extract_code = asyncio.run(self._share(file_id))
        return UploadOutcome(self.code, file_id, share_url, extract_code, remote_path)


def connector_statuses(
    settings: Settings | None = None,
    db: Session | None = None,
) -> list[ConnectorStatus]:
    settings = settings or get_settings()
    return [baidu_connector_status(settings), quark_connector_status(settings, db)]


def _connector(provider_code: str, settings: Settings | None = None):
    if provider_code == "baidu":
        return BaiduCloudConnector(settings)
    if provider_code == "quark":
        return QuarkCloudConnector(settings)
    raise CloudConnectorError("暂不支持该网盘的自动上传")


def _finish_upload_task(db: Session, task: BackgroundTask, outcome: UploadOutcome) -> None:
    resource_id = int(task.payload["resource_id"])
    resource = db.get(Resource, resource_id)
    if not resource:
        raise CloudConnectorError("对应图书已被删除，未回填分享链接")
    try:
        link = add_or_replace_link(db, resource.id, outcome.share_url, outcome.extract_code)
    except DuplicateLinkError as exc:
        existing_channel = db.get(ResourceChannel, exc.existing_link.channel_id)
        if not existing_channel or existing_channel.resource_id != resource.id:
            raise
        link = exc.existing_link
    channel = db.get(ResourceChannel, link.channel_id)
    if channel:
        channel.provider_file_id = outcome.provider_file_id

    local_path = Path(str(task.payload["local_path"]))
    file_row = db.scalar(
        select(ResourceFile).where(ResourceFile.resource_id == resource.id, ResourceFile.local_path == str(local_path))
    )
    if not file_row:
        file_row = ResourceFile(
            resource_id=resource.id,
            file_name=local_path.name,
            file_format=local_path.suffix.lstrip(".").upper() or None,
            file_size=local_path.stat().st_size if local_path.exists() else None,
            local_path=str(local_path),
            source_type="cloud_upload",
        )
        db.add(file_row)
    formats = {item.strip().upper() for item in (resource.formats or "").replace("·", " ").split() if item.strip()}
    if file_row.file_format:
        formats.add(file_row.file_format)
    resource.formats = " · ".join(sorted(formats)) or None
    try:
        check_link(db, link)
    except Exception as exc:  # 检测失败不能抹掉已经生成的官方分享链接
        link.last_error = _safe_error_text(exc)
    if task.payload.get("publish_after_upload") and link.status == "active":
        resource.publish_status = "published"
        resource.published_at = resource.published_at or utcnow()
    task.status = "completed"
    task.result = {
        "provider_code": outcome.provider_code,
        "provider_file_id": outcome.provider_file_id,
        "share_url": outcome.share_url,
        "extract_code": outcome.extract_code,
        "remote_path": outcome.remote_path,
        "link_id": link.id,
        "link_status": link.status,
    }
    task.error_message = None
    if outcome.provider_code == "quark":
        previous = _stored_authorization(db, "quark")
        _record_authorization(
            db,
            "quark",
            "authorized",
            "夸克账号授权有效",
            str(previous.get("account_name") or "").strip() or None,
        )


def _set_task_error(task_id: int, status: str, message: str) -> None:
    with SessionLocal() as db:
        task = db.get(BackgroundTask, task_id)
        if task:
            task.status = status
            task.error_message = _safe_error_text(message)
            if status == "needs_auth" and task.payload.get("provider_code") == "quark":
                _record_authorization(db, "quark", "required", str(message))
            db.commit()


def process_next_cloud_task(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    with SessionLocal() as db:
        task = db.scalar(
            select(BackgroundTask)
            .where(
                BackgroundTask.status == "pending",
                BackgroundTask.task_type.in_(["cloud_upload", "cloud_auth"]),
            )
            .order_by(BackgroundTask.id)
        )
        if not task:
            return False
        claimed = db.execute(update(BackgroundTask).where(BackgroundTask.id == task.id, BackgroundTask.status == "pending").values(status="running")).rowcount
        if claimed != 1:
            db.rollback()
            return False
        db.commit()
        task_id = task.id
        task_type = task.task_type
        payload = dict(task.payload)

    try:
        connector = _connector(str(payload.get("provider_code") or ""), settings)
        if task_type == "cloud_auth":
            if not isinstance(connector, QuarkCloudConnector):
                raise CloudConnectorError("该网盘不使用本地授权任务")
            auth_result = connector.authorize()
            auth_data = auth_result.get("data") if isinstance(auth_result.get("data"), dict) else {}
            user_info = auth_data.get("userInfo") if isinstance(auth_data.get("userInfo"), dict) else {}
            account_name = str(user_info.get("nickname") or "").strip() or None
            with SessionLocal() as db:
                task = db.get(BackgroundTask, task_id)
                if task:
                    task.status = "completed"
                    task.result = {"message": "已授权", "account_name": account_name}
                    task.error_message = None
                    _record_authorization(db, "quark", "authorized", "夸克账号授权有效", account_name)
                    db.commit()
            return True

        local_path = validate_source_path(str(payload.get("local_path") or ""), settings)
        outcome = connector.upload_and_share(local_path, str(payload.get("resource_title") or local_path.stem))
        with SessionLocal() as db:
            task = db.get(BackgroundTask, task_id)
            if not task:
                return True
            _finish_upload_task(db, task, outcome)
            db.commit()
    except ConnectorAuthRequired as exc:
        _set_task_error(task_id, "needs_auth", str(exc))
    except (CloudConnectorError, DuplicateLinkError, ValueError, OSError, httpx.HTTPError) as exc:
        _set_task_error(task_id, "failed", str(exc))
    except Exception as exc:  # 保留未知错误供后台排查，但不让队列停止
        _set_task_error(task_id, "failed", f"未预期错误：{exc}")
    return True


async def cloud_upload_worker_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    while not stop_event.is_set():
        processed = await asyncio.to_thread(process_next_cloud_task, settings)
        if processed:
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.cloud_upload_poll_seconds)
        except TimeoutError:
            pass


def queue_quark_auth_task(db: Session, admin_id: int | None = None) -> BackgroundTask:
    existing = db.scalar(
        select(BackgroundTask).where(
            BackgroundTask.task_type == "cloud_auth",
            BackgroundTask.status.in_(["pending", "running"]),
        )
    )
    if existing:
        return existing
    task = BackgroundTask(
        task_type="cloud_auth",
        status="pending",
        payload={"provider_code": "quark", "created_by_id": admin_id},
    )
    db.add(task)
    db.flush()
    _record_authorization(db, "quark", "checking", "正在确认夸克账号授权")
    return task


def retry_cloud_task(db: Session, task_id: int) -> BackgroundTask | None:
    task = db.get(BackgroundTask, task_id)
    if not task or task.task_type not in {"cloud_upload", "cloud_auth"}:
        return None
    if task.status not in {"failed", "needs_auth"}:
        return task
    task.status = "pending"
    task.error_message = None
    task.result = {}
    db.flush()
    return task


def cancel_cloud_task(db: Session, task_id: int) -> BackgroundTask | None:
    task = db.get(BackgroundTask, task_id)
    if not task or task.task_type not in {"cloud_upload", "cloud_auth"}:
        return None
    if task.status == "pending":
        task.status = "cancelled"
        db.flush()
    return task


def delete_cloud_task(db: Session, task_id: int) -> BackgroundTask | None:
    task = db.get(BackgroundTask, task_id)
    if not task or task.task_type not in {"cloud_upload", "cloud_auth"}:
        return None
    if task.status not in {"failed", "needs_auth", "cancelled"}:
        return task
    db.delete(task)
    db.flush()
    return task


def clear_problem_cloud_tasks(db: Session) -> int:
    tasks = list(
        db.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type.in_(["cloud_upload", "cloud_auth"]),
                BackgroundTask.status.in_(["failed", "needs_auth", "cancelled"]),
            )
        )
    )
    for task in tasks:
        db.delete(task)
    db.flush()
    return len(tasks)


def retry_problem_cloud_tasks(db: Session, provider_code: str = "quark") -> int:
    tasks = list(
        db.scalars(
            select(BackgroundTask).where(
                BackgroundTask.task_type == "cloud_upload",
                BackgroundTask.status.in_(["failed", "needs_auth"]),
            )
        )
    )
    retried = 0
    for task in tasks:
        if task.payload.get("provider_code") != provider_code:
            continue
        task.status = "pending"
        task.error_message = None
        task.result = {}
        retried += 1
    db.flush()
    return retried


def upload_progress(db: Session) -> UploadProgress | None:
    """统计最近一批上传任务的整体进度。

    以最新一条任务所属的 batch_key 为口径；早期没有 batch_key 的任务
    全部归为一批，避免升级后看不到历史进度。
    """
    latest = db.scalar(
        select(BackgroundTask)
        .where(BackgroundTask.task_type == "cloud_upload")
        .order_by(BackgroundTask.id.desc())
    )
    if latest is None:
        return None
    key = str(latest.payload.get("batch_key") or "")
    statement = select(BackgroundTask.status, func.count()).where(
        BackgroundTask.task_type == "cloud_upload"
    )
    if key:
        statement = statement.where(BackgroundTask.payload["batch_key"].as_string() == key)
    counts = dict(db.execute(statement.group_by(BackgroundTask.status)).all())
    progress = UploadProgress(
        completed=int(counts.get("completed", 0)),
        running=int(counts.get("running", 0)),
        pending=int(counts.get("pending", 0)),
        failed=int(counts.get("failed", 0)),
        needs_auth=int(counts.get("needs_auth", 0)),
        cancelled=int(counts.get("cancelled", 0)),
        batch_key=key,
    )
    progress.total = (
        progress.completed
        + progress.running
        + progress.pending
        + progress.failed
        + progress.needs_auth
        + progress.cancelled
    )
    progress.active = bool(progress.pending or progress.running)
    progress.percent = round(progress.completed / progress.total * 100) if progress.total else 0
    return progress


CLOUD_TASK_LABELS = {
    "pending": "等待上传",
    "running": "正在处理",
    "needs_auth": "等待授权",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
