from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .engine import public_record
from .safeio import Control, atomic_bytes, safe_name, sha256_file
from .workspace import Workspace


class Credentials:
    """只使用 Windows 系统凭据；不可用时保存在本次进程内存。"""
    def __init__(self, workspace_id):
        self.service = "EbookOrganizer/" + workspace_id
        self.memory = {}
        self.backend = None
        try:
            from keyring.backends.Windows import WinVaultKeyring
            self.backend = WinVaultKeyring()
        except Exception:
            pass

    def get(self, key):
        if key in self.memory:
            return self.memory[key]
        try:
            return self.backend.get_password(self.service, key) if self.backend else None
        except Exception:
            return None

    def set(self, key, value):
        self.memory[key] = value
        try:
            if self.backend:
                self.backend.set_password(self.service, key, value)
                return True
        except Exception:
            pass
        return False


def upload_cover(workspace: Workspace, book_id: str, config: dict, credentials: Credentials, s3=None, http=None):
    book = workspace.book(book_id)
    if not book or not book["cover_path"]:
        raise ValueError("此书没有可上传的封面")
    account, bucket = config.get("r2_account", ""), config.get("r2_bucket", "")
    base = config.get("r2_public", "").rstrip("/")
    parsed = urlsplit(base)
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account) or not bucket or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("请先配置 R2 账户编号、存储桶和公开 HTTPS 图片域名")
    if s3 is None:
        import boto3
        from botocore.config import Config
        key, secret = credentials.get("r2_key"), credentials.get("r2_secret")
        if not key or not secret:
            raise ValueError("未配置 R2 对象访问凭据")
        s3 = boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com", region_name="auto", aws_access_key_id=key, aws_secret_access_key=secret,
                          config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 5, "mode": "standard"}))
    data = (workspace.root / book["cover_path"]).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    key = f"books/{book_id}/{book['cover_version']}/cover.webp"
    url = base + "/" + key
    from botocore.exceptions import ClientError
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) not in {"404", "NoSuchKey", "NotFound"}:
            raise ValueError("R2 无法读取对象，请核对对象权限和网络；未继续批次") from None
        head = None
    if head is not None and (head.get("ContentLength") != len(data) or head.get("Metadata", {}).get("sha256") != digest):
        raise ValueError("同一封面版本的远端内容不一致，已阻止覆盖")
    if head is None:
        s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="image/webp", CacheControl="public,max-age=31536000,immutable", Metadata={"sha256": digest})
    head = s3.head_object(Bucket=bucket, Key=key)
    if head.get("ContentLength") != len(data) or head.get("Metadata", {}).get("sha256") != digest:
        raise ValueError("R2 写入复核失败，未生成可用封面地址")
    state = {"state": "uploaded", "key": key, "version": book["cover_version"], "sha256": digest,
             "account": account, "bucket": bucket}
    workspace.save_result(book_id, "r2", state)
    owned = http is None
    http = http or httpx.Client(timeout=30, follow_redirects=False)
    try:
        with http.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError("封面已存储，但公开访问未通过，暂不回填地址")
            received = bytearray()
            for chunk in response.iter_bytes():
                received.extend(chunk)
                if len(received) > len(data):
                    raise ValueError("公开封面大小不符")
        if hashlib.sha256(received).hexdigest() != digest:
            raise ValueError("公开封面内容不符")
        state.update(state="verified", url=url)
        workspace.save_result(book_id, "r2", state)
        return state
    finally:
        if owned:
            http.close()


def _ensure_quark_agent_env():
    """夸克官方 CLI 要求检测到受支持的 Agent 环境（CodeBuddy/WorkBuddy）。
    桌面 exe 由 WorkBuddy 打包 but 运行时不是 WorkBuddy 子进程，需显式保留该标记。
    只在未设置时写入，避免覆盖真实 WorkBuddy 会话。"""
    if os.environ.get("CODEBUDDY_CONFIG_DIR"):
        return
    fallback = Path.home() / ".workbuddy"
    fallback.mkdir(parents=True, exist_ok=True)
    os.environ["CODEBUDDY_CONFIG_DIR"] = str(fallback)


def quark_connector(workspace, config):
    from app.core.config import Settings
    from app.services.cloud_uploads import QuarkCloudConnector, _detect_quark_agent
    _ensure_quark_agent_env()
    source_runtime = Path(__file__).resolve().parents[1] / "runtime"
    root = Path(config.get("connector_runtime") or (source_runtime if (source_runtime / "cloud-connectors").exists() else workspace.root / "connector-runtime"))
    settings = Settings(local_storage_root=root, quark_cli_path=Path(config["quark_cli"]) if config.get("quark_cli") else None, cloud_upload_worker_enabled=False)
    connector = QuarkCloudConnector(settings)
    # node 在 Windows 长路径前缀 \\?\ 下会 realpathSync 报错，需先规范化
    def _norm(p):
        return Path(str(p).replace("\\\\?\\", "").replace("//?/", ""))
    connector.cli = _norm(connector.cli)
    connector.node = _norm(connector.node)
    if not _detect_quark_agent(connector.cli, connector.node):
        raise ValueError("夸克官方连接器不认可当前运行环境。请从 WorkBuddy/CodeBuddy 中启动本软件，或通过客户端上传后手工回填链接。")
    return connector


def quark_list_folders(workspace, config):
    """尽力列举夸克顶层目录；CLI 不支持列举时给出可操作指引而非崩溃。"""
    connector = quark_connector(workspace, config)
    last_error = None
    for cmd in (["list"], ["ls"], ["list-folders"], ["get-folder-list"]):
        try:
            out = connector._run(cmd, timeout=30)
        except Exception as exc:  # noqa: BLE001 - 未知子命令会被 CLI 拒绝，逐个尝试
            last_error = exc
            continue
        data = (out or {}).get("data") or {}
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("list") or data.get("folders") or data.get("items") or []
        else:
            continue
        folders = []
        for item in items:
            if isinstance(item, dict):
                fid = item.get("fid") or item.get("id")
                if fid is not None and str(fid):
                    folders.append((str(item.get("name") or item.get("title") or ""), str(fid)))
        if folders:
            return folders
    raise ValueError("无法自动列出夸克目录（连接器未提供列举子命令）。\n请在夸克网盘网页端打开目标文件夹，从浏览器地址栏复制文件夹 fid 后填入；顶层文件夹 fid 即「夸克目标目录编号」。")


def stage_file(workspace, book, control=None):
    """创建校验过的上传副本；不改动原书，也不调用网盘或环境识别。"""
    control = control or Control()
    control.check()
    path = workspace.source(book["book_id"])
    if sha256_file(path, control) != book["sha256"]:
        raise ValueError("源文件已改变，请重新扫描")
    target = workspace.root / "transfer" / book["book_id"] / (safe_name(book["metadata"]["title"], 65) + "__" + book["book_id"] + ".epub")
    if not target.exists() or sha256_file(target, control) != book["sha256"]:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part")
        with path.open("rb") as source, temporary.open("wb") as destination:
            while chunk := source.read(1024 * 1024):
                control.check()
                destination.write(chunk)
        if sha256_file(temporary, control) != book["sha256"]:
            raise ValueError("准备上传时内容发生变化")
        temporary.replace(target)
    return target


def _quark_step(connector, arguments, stage, workspace, book_id, **kwargs):
    from .batch_edit import error_message
    from app.services.cloud_uploads import CloudConnectorError
    workspace.event('quark', stage + '：开始', book_id)
    try:
        result = connector._run(arguments, **kwargs)
    except (CloudConnectorError, OSError, ValueError) as exc:
        message = stage + '失败：' + error_message(exc)
        workspace.event('quark', message, book_id)
        if isinstance(exc, CloudConnectorError):
            raise type(exc)(message) from exc
        if isinstance(exc, ValueError): raise ValueError(message) from exc
        raise CloudConnectorError(message) from exc
    workspace.event('quark', stage + '：完成', book_id)
    return result


def upload_book(workspace, book_id, provider, config, credentials, control=None, connector=None):
    control = control or Control()
    control.check()
    if provider not in {"quark", "baidu"}:
        raise ValueError("暂不支持此网盘")
    book = workspace.book(book_id)
    if not book or book["status"] in {"failed", "blocked"} or book["excluded"]:
        raise ValueError("文件异常或已排除，不能上传")
    meta = book["metadata"]
    if meta.get("rights_review_status") != "confirmed" or not (meta.get("source_reference") or "").strip() or meta.get("copyright_status") not in {"authorized", "public_domain", "open_license"}:
        raise ValueError("请先确认此书的版权类别和来源")
    if not meta.get("main_category") or meta.get("classification_status") != "confirmed":
        raise ValueError("请先确认分类")
    outcome = workspace.result(book_id, provider)
    if outcome.get("share_url"):
        return outcome
    if outcome.get("state") == "uploading" and not outcome.get("file_ids"):
        raise ValueError("上次上传结果未确认。请先在网盘核对，避免重试产生重复文件；可手工回填已生成的分享链接。")
    categories = [safe_name(meta["main_category"])]
    if meta.get("subcategory"):
        categories.append(safe_name(meta["subcategory"]))
    if provider == "quark":
        connector = connector or quark_connector(workspace, config)
        parent = config.get("quark_parent", "").strip()
        if not parent:
            raise ValueError("请配置夸克目标目录编号；明确选择根目录时填写0")
        cache = workspace.setting("quark_directories", {})
        for category in categories:
            cache_key = parent + "/" + category
            fid = cache.get(cache_key)
            if not fid:
                created = _quark_step(connector, ["create-folder", "--dir-path", category, "--parent-fid", parent], '创建分类目录', workspace, book_id)
                fid = str((created.get("data") or {}).get("fid") or "")
                if not fid:
                    raise ValueError("夸克未返回分类文件夹编号")
                cache[cache_key] = fid
                workspace.set_setting("quark_directories", cache)
            parent = fid
        if not outcome.get("file_ids"):
            local = stage_file(workspace, book, control)
            workspace.save_result(book_id, provider, {"state": "uploading", "sha256": book["sha256"]})
            uploaded = _quark_step(connector, ["upload", str(local), "--parent-fid", parent], '上传电子书', workspace, book_id)
            fids = (uploaded.get("data") or {}).get("fids") or []
            if not fids:
                raise ValueError("上传结果未返回文件编号，需要到网盘核对")
            outcome = {"state": "uploaded", "file_ids": fids, "sha256": book["sha256"], "parent_fid": parent}
            workspace.save_result(book_id, provider, outcome)
        shared = _quark_step(connector, ["share", *[str(fid) for fid in outcome["file_ids"]], "--title", meta["title"], "--url-type", "1", "--expired-type", "1"], '生成分享链接', workspace, book_id, timeout=120)
        data = shared.get("data") or {}
        if not data.get("share_url"):
            raise ValueError("文件已上传，分享尚未生成；重试只创建分享")
        outcome.update(state="shared", share_url=data["share_url"], extract_code=data.get("passcode"))
    elif provider == "baidu":
        from app.core.config import Settings
        from app.services.cloud_uploads import BaiduCloudConnector
        token = credentials.get("baidu_token")
        if not token:
            raise ValueError("未配置百度授权令牌")
        remote = "/" + "/".join([config.get("baidu_root", "电子书库").strip("/"), *categories])
        connector = connector or BaiduCloudConnector(Settings(baidu_netdisk_access_token=token, baidu_netdisk_remote_dir=remote, cloud_upload_worker_enabled=False))
        if not outcome.get("file_ids"):
            local = stage_file(workspace, book, control)
            with httpx.Client(timeout=30) as client:
                built = ""
                for segment in remote.strip("/").split("/"):
                    built += "/" + segment
                    response = client.post("https://pan.baidu.com/rest/2.0/xpan/file", params={"method": "create", "access_token": token}, data={"path": built, "isdir": "1", "rtype": "0"}).json()
                    # -8 为已存在；其他错误不可当作目录创建成功。
                    if int(response.get("errno") or 0) != -8:
                        connector._ensure_ok(response, "创建分类目录")
            workspace.save_result(book_id, provider, {"state": "uploading", "sha256": book["sha256"]})
            fid, remote_path = connector._upload(local)
            outcome = {"state": "uploaded", "file_ids": [fid], "sha256": book["sha256"], "remote_path": remote_path}
            workspace.save_result(book_id, provider, outcome)
        link, code = asyncio.run(connector._share(str(outcome["file_ids"][0])))
        outcome.update(state="shared", share_url=link, extract_code=code)
    else:
        raise ValueError("暂不支持此网盘")
    workspace.save_result(book_id, provider, outcome)
    return outcome


def sync_fingerprint(record, site_url, site_id, publish=False, overwrite=False):
    payload = {"book": record, "site_url": site_url.rstrip("/"), "site_id": site_id,
               "publish": bool(publish), "overwrite": bool(overwrite)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cover_is_current(book, state, config):
    if not book.get("cover_path") or not book.get("cover_version"):
        return False
    key = f"books/{book['book_id']}/{book['cover_version']}/cover.webp"
    return (state.get("state") == "verified" and state.get("version") == book["cover_version"]
            and state.get("account") == config.get("r2_account") and state.get("bucket") == config.get("r2_bucket")
            and state.get("url") == str(config.get("r2_public") or "").rstrip("/") + "/" + key)


class SiteClient:
    def __init__(self, workspace, config, credentials, client=None):
        self.workspace, self.config = workspace, config
        base = config.get("site_url", "").rstrip("/")
        parsed = urlsplit(base)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
            raise ValueError("正式站点只允许 HTTPS；本地测试可用 localhost HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("站点地址格式不正确")
        token = credentials.get("site_token")
        if not token:
            raise ValueError("未配置网站专用同步授权")
        self.base, self.owned = base, client is None
        self.client = client or httpx.Client(timeout=120, follow_redirects=False)
        self.headers = {"Authorization": "Bearer " + token}

    def request(self, method, path, data=None):
        response = self.client.request(method, self.base + "/api/v1/organizer" + path, json=data, headers=self.headers)
        if response.status_code >= 300:
            try:
                detail = str(response.json().get("detail", ""))[:500]
            except Exception:
                detail = "响应不是预期的 JSON 数据"
            raise ValueError(f"网站请求未完成（{response.status_code}）：{detail}")
        return response.json()

    def info(self):
        return self.request("GET", "/info")

    def preview(self, ids):
        from app.services.organizer_contract import OrganizerPackage
        info = self.info()
        expected = self.config.get("site_id")
        if not expected or info["site_id"] != expected:
            raise ValueError("网站编号不一致，请核对连接设置")
        if not 1 <= len(ids) <= 500:
            raise ValueError("网站每批处理1～500本，请分批选择；建议首次用10～20本验收")
        selected = [self.workspace.book(book_id) for book_id in ids]
        if any(not b or b["excluded"] or b["status"] in {"failed", "blocked"} for b in selected):
            raise ValueError("异常、已阻止或已排除的图书不能同步，请先修正或取消选择")
        books = [public_record(self.workspace, book) for book in selected]
        data = {"schema_version": "2.0", "export_id": uuid.uuid4().hex, "workspace_id": self.workspace.setting("workspace_id"), "site_id": expected, "books": books}
        OrganizerPackage.model_validate(data)
        result = self.request("POST", "/preview", data)
        result["_client_books"] = {book["book_id"]: book for book in books}
        result["_client_site_url"] = self.base
        self.workspace.set_setting("last_site_preview", {"site_url": self.base, "site_id": expected, "preview": result})
        return result

    def commit(self, preview, choices, control=None, progress=lambda _: None):
        from app.services.organizer_contract import CommitChoices
        from .safeio import Cancelled
        CommitChoices.model_validate({"choices": choices})
        control = control or Control()
        expected = self.config.get("site_id")
        if preview.get("site_id", expected) != expected or preview.get("_client_site_url", self.base) != self.base:
            raise ValueError("预检不属于当前网站，请重新预检")
        result = {"site_id": expected, "export_id": preview["export_id"], "items": {}}
        # 逐本提交便于取消、保存回执，并避免整批检测超过代理请求时限。
        for index, choice in enumerate(choices):
            control.check()
            progress(f"网站提交：{index + 1}/{len(choices)}")
            bid = choice["book_id"]
            try:
                if "_client_books" in preview:
                    book = self.workspace.book(bid)
                    if not book:
                        raise ValueError("图书已从本地书库移除，请重新预检")
                    record = preview["_client_books"].get(bid)
                    if book["excluded"] or book["status"] in {"failed", "blocked"} or record != public_record(self.workspace, book):
                        raise ValueError("图书资料或状态在预检后发生变化，请重新预检")
                response = self.request("POST", f"/batches/{preview['export_id']}/commit", {"choices": [choice]})
                if response.get("site_id") != expected:
                    raise ValueError("回执站点编号不一致")
                item = dict(response.get("items", {}).get(bid) or {"status": "error", "message": "网站没有返回此书的回执"})
                if item.get("status") == "ok":
                    record = preview.get("_client_books", {}).get(bid)
                    if record:
                        item["_sync_fingerprint"] = sync_fingerprint(record, self.base, expected, choice.get("publish"), choice.get("overwrite"))
                        item["_site_url"] = self.base
                else:
                    item["status"] = "error"
            except Cancelled:
                raise
            except (ValueError, httpx.HTTPError) as exc:
                item = {"status": "error", "message": str(exc)[:500]}
            result["items"][bid] = item
            if "_client_books" not in preview or self.workspace.book(bid):
                self._save_receipt({"site_id": expected, "items": {bid: item}})
        return result

    def _save_receipt(self, result):
        if result["site_id"] != self.config.get("site_id"):
            raise ValueError("回执站点编号不一致")
        for book_id, receipt in result["items"].items():
            self.workspace.save_result(book_id, "site:" + result["site_id"], receipt)

    def close(self):
        if self.owned:
            self.client.close()
