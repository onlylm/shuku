"""Linux 受限维护服务。仅接受域名、固定仓库正式版本、备份三种操作。"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time

from scripts.maintenance_protocol import PROTOCOL, REPOSITORY_URL, current_version, release_info, validate_job, version_key
from scripts.server_backup import verify
from scripts.server_config import hostname, read_config, validate

ROOT = Path(__file__).resolve().parents[1]


def atomic_text(path: Path, text: str, mode: int = 0o644):
    # 写入目录由 root 管理；网页仅能写 requests，不可替换状态或配置。
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def public_addresses(domain: str) -> list[str]:
    hostname(domain)
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError("域名必须解析到公网地址，不允许访问内网、回环或保留地址")
    return addresses


def https_get(domain: str, path: str, headers: dict | None = None) -> tuple[int, bytes]:
    # 固定刚验证的公网 IP，并用原域名校验证书，避免 DNS 重绑定/内网探测。
    ips = public_addresses(domain)
    last_error = None
    for ip in ips:
        connection = http.client.HTTPSConnection(domain, timeout=10, context=ssl.create_default_context())
        try:
            sock = socket.create_connection((ip, 443), timeout=10)
            try:
                connection.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=domain)
            except Exception:
                sock.close()
                raise
            connection.request("GET", path, headers={"Host": domain, **(headers or {})})
            response = connection.getresponse()
            return response.status, response.read(4096)
        except OSError as exc:
            last_error = exc
        finally:
            connection.close()
    raise ValueError("域名 HTTPS 验证失败，请检查解析、证书和80/443端口") from last_error


def alias_config(primary: str, aliases: list[str]) -> str:
    primary = hostname(primary)
    blocks = []
    for alias in dict.fromkeys(aliases):
        alias = hostname(alias)
        if alias != primary:
            # 308 保留桌面 API 的 POST 方法；用户仍应更新桌面软件中的主域名。
            blocks.append(f"{alias} {{\n\tredir https://{primary}{{uri}} 308\n}}\n")
    return "\n".join(blocks)


class HostMaintenance:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.config = self.root / "deploy" / ".env"
        self.control = self.root / "deploy" / "control"
        self.domains = self.root / "deploy" / "domains"
        self.job = None
        self.result = {}
        self.recovery_required = False

    def command(self, args: list[str], *, capture=False, timeout=3600, stdin=None):
        options = {"cwd": self.root, "timeout": timeout, "check": True, "stdin": stdin}
        if capture:
            options.update(stdout=subprocess.PIPE, text=True)
        # deploy.sh 已持有部署锁；仅向自己的部署子进程传递锁描述符。
        if args[:2] == ["bash", str(self.root / "deploy.sh")] and os.environ.get("EBOOK_DEPLOY_LOCK_FD") == "9":
            options["pass_fds"] = (9,)
        return subprocess.run(args, **options).stdout if capture else subprocess.run(args, **options)

    def compose(self, *args, **kwargs):
        return self.command(["docker", "compose", "--project-directory", str(self.root / "deploy"),
            "--env-file", str(self.config), "-f", str(self.root / "deploy" / "compose.yml"), *args], **kwargs)

    def git(self, *args, **kwargs):
        return self.command(["git", "-c", "safe.directory=" + str(self.root), *args], **kwargs)

    def prepare(self):
        for directory in (self.control, self.control / "requests", self.control / "status", self.domains):
            if directory.is_symlink():
                raise ValueError("维护目录不允许使用符号链接")
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o755)
        uid = int(self.compose("run", "--rm", "-T", "--no-deps", "--entrypoint", "id", "web", "-u", capture=True).strip())
        gid = int(self.compose("run", "--rm", "-T", "--no-deps", "--entrypoint", "id", "web", "-g", capture=True).strip())
        if os.name == "posix":
            os.chown(self.control / "requests", uid, gid)
        (self.control / "requests").chmod(0o700)
        values = read_config(self.config)
        atomic_text(self.domains / "aliases.caddy", alias_config(values["SITE_DOMAIN"], values.get("SITE_ALIASES", "").split(",") if values.get("SITE_ALIASES") else []))
        gate = self.domains / "gate.conf"
        if not gate.exists():
            atomic_text(gate, "# 正常提供服务\n")

    def heartbeat(self):
        atomic_text(self.control / "status" / "heartbeat.json", json.dumps({"protocol": PROTOCOL, "time": time.time()}))

    def update_status(self, status: str, message: str, **extra):
        self.result.update(id=self.job["id"], label={"update": "系统更新", "domains": "域名变更", "backup": "网站备份"}[self.job["kind"]],
            status=status, status_label={"running": "处理中", "completed": "已完成", "failed": "未完成", "rolled_back": "已恢复旧版本"}[status],
            message=message, updated_at=time.time(), **extra)
        atomic_text(self.control / "status" / ("job-" + self.job["id"] + ".json"), json.dumps(self.result, ensure_ascii=False))

    def reload_caddy(self):
        self.compose("exec", "-T", "caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")
        self.compose("exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile")

    def save_config(self, values: dict):
        validate(values)
        atomic_text(self.config, "# 私密部署配置，禁止上传 GitHub\n" + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", 0o600)

    def apply_domains(self, payload: dict):
        previous = read_config(self.config)
        old_primary = previous["SITE_DOMAIN"]
        if payload["previous_primary"] != old_primary:
            raise ValueError("当前域名已发生变化，请刷新后台后重新确认")
        primary = payload["primary"]
        aliases = list(dict.fromkeys([*payload["aliases"], old_primary]))
        aliases = [x for x in aliases if x != primary]
        old_aliases = previous.get("SITE_ALIASES", "").split(",") if previous.get("SITE_ALIASES") else []
        added = [x for x in [primary, *aliases] if x not in [old_primary, *old_aliases]]
        for domain in added:
            public_addresses(domain)
        nonce = secrets.token_hex(32)
        staging = self.domains / "verify.caddy"
        # 在切换主域名前先给新域名提供站点归属证明，旧网站继续服务。
        text = "\n".join(f'{d} {{\n\trespond /api/v1/domain-verification "{nonce}"\n\trespond "域名验证中" 503\n}}' for d in added)
        self.update_status("running", "正在检查解析、申请证书并验证新域名是否指向本站")
        try:
            atomic_text(staging, text)
            self.reload_caddy()
            for domain in added:
                for attempt in range(12):
                    try:
                        code, body = https_get(domain, "/api/v1/domain-verification")
                        if code == 200 and body.decode("utf-8") == nonce:
                            break
                    except (OSError, ValueError):
                        pass
                    if attempt == 11:
                        raise ValueError("新域名验证未通过，原主域名保持不变；请检查解析和证书后重试")
                    time.sleep(5)
            updated = {**previous, "SITE_DOMAIN": primary, "SITE_ALIASES": ",".join(aliases)}
            atomic_text(self.control / "status" / ("recovery-" + self.job["id"] + ".json"),
                json.dumps({"kind": "domains", "previous_config": previous}), 0o600)
            self.save_config(updated)
            atomic_text(staging, "")
            atomic_text(self.domains / "aliases.caddy", alias_config(primary, aliases))
            self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "360", "web", "caddy")
            code, _ = https_get(primary, "/api/v1/ready")
            if code != 200:
                raise ValueError("新域名应用检查未通过")
        except Exception:
            self.save_config(previous)
            atomic_text(staging, "")
            atomic_text(self.domains / "aliases.caddy", alias_config(old_primary, old_aliases))
            self.recovery_required = True
            self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "360", "web", "caddy")
            self.reload_caddy()
            self.recovery_required = False
            raise
        self.update_status("completed", f"域名已生效：https://{primary}；旧入口保留。请更新桌面软件的网站地址。")

    def backup(self, keep_stopped=False) -> Path:
        before = set((self.root / "backups").glob("*/manifest.json"))
        self.command(["bash", str(self.root / "deploy.sh"), "maintenance-backup" if keep_stopped else "backup"])
        created = set((self.root / "backups").glob("*/manifest.json")) - before
        if len(created) != 1:
            raise ValueError("没有找到本次完整备份，停止后续操作")
        snapshot = created.pop().parent
        verify(snapshot)
        self.update_status("running", "完整备份已生成并校验", backup=str(snapshot))
        return snapshot

    def maintenance_gate(self, nonce: str | None):
        text = '# 正常提供服务\n' if not nonce else f'@maintenance {{\n\tnot header X-Shuku-Maintenance {nonce}\n}}\nrespond @maintenance "网站正在维护，请稍后重试。" 503\n'
        atomic_text(self.domains / "gate.conf", text)
        self.reload_caddy()

    def smoke(self, nonce: str):
        primary = read_config(self.config)["SITE_DOMAIN"]
        for path in ("/api/v1/ready", "/", "/books", "/admin/login", "/robots.txt", "/sitemap.xml"):
            status, _ = https_get(primary, path, {"X-Shuku-Maintenance": nonce})
            if status != 200:
                raise ValueError("升级后网站可用性检查未通过")

    def restore_snapshot(self, snapshot: Path, old_revision: str, old_image: str, image_name: str):
        verify(snapshot)
        self.compose("stop", "web")
        self.git("switch", "--detach", old_revision)
        self.save_config(read_config(snapshot / "deploy.env"))
        self.command(["docker", "tag", old_image, image_name])
        # 数据库及文件都恢复到停站后的同一快照；恢复中不会开放写入。
        # 先清除失败迁移新建的表，否则旧备份不含这些表，下一次升级会撞表。
        listing = self.compose("exec", "-T", "db", "sh", "-c",
            'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" "$MYSQL_DATABASE" -N -B -e "SHOW FULL TABLES"', capture=True)
        statements = ["SET FOREIGN_KEY_CHECKS=0"]
        for line in listing.strip().splitlines():
            name, kind = line.split("\t")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name) or kind not in {"BASE TABLE", "VIEW"}:
                raise ValueError("发现非标准数据库对象，保持维护状态，请人工恢复")
            statements.append(f"DROP {'VIEW' if kind == 'VIEW' else 'TABLE'} `{name}`")
        statements.append("SET FOREIGN_KEY_CHECKS=1")
        self.compose("exec", "-T", "db", "sh", "-c",
            'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" "$MYSQL_DATABASE" -e "$1"',
            "maintenance", ";".join(statements))
        with gzip.open(snapshot / "database.sql.gz", "rb") as source:
            process = subprocess.Popen(["docker", "compose", "--project-directory", str(self.root / "deploy"),
                "--env-file", str(self.config), "-f", str(self.root / "deploy/compose.yml"), "exec", "-T", "db", "sh", "-c",
                'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u"$MYSQL_USER" "$MYSQL_DATABASE"'], stdin=subprocess.PIPE, cwd=self.root)
            try:
                import shutil
                shutil.copyfileobj(source, process.stdin)
                process.stdin.close()
                if process.wait(timeout=600) != 0:
                    raise ValueError("数据库恢复失败，网站保持维护状态")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.compose("run", "--rm", "-T", "--no-deps", "-v", str(snapshot) + ":/restore:ro", "--entrypoint", "python", "web",
            "-m", "scripts.server_backup", "restore-files", "/restore/files.tar.gz")
        self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "360", "web", "caddy")

    def update_release(self, payload: dict):
        if self.git("status", "--porcelain", capture=True).strip():
            raise ValueError("服务器源码有未提交修改，已停止更新，未覆盖任何源码")
        release = release_info(payload["tag"])
        if release["sha"] != payload["sha"]:
            raise ValueError("正式版本校验信息已变化，请重新检查并确认版本")
        if version_key(release["tag"]) <= version_key(current_version()):
            raise ValueError("该版本不比当前版本新，已停止重复更新或降级")
        self.update_status("running", "正在验证正式版本并准备升级")
        self.git("fetch", "--no-tags", REPOSITORY_URL, payload["sha"])
        if self.git("rev-parse", "FETCH_HEAD", capture=True).strip() != payload["sha"]:
            raise ValueError("下载的代码与已确认版本不一致")
        target_version = self.git("show", payload["sha"] + ":VERSION", capture=True).strip()
        if target_version != payload["tag"][1:]:
            raise ValueError("发布标签与源码版本不一致，已停止升级")
        old_revision = self.git("rev-parse", "HEAD", capture=True).strip()
        container = self.compose("ps", "-q", "web", capture=True).strip()
        old_image = self.command(["docker", "inspect", "--format", "{{.Image}}", container], capture=True).strip()
        image_name = self.command(["docker", "inspect", "--format", "{{.Config.Image}}", container], capture=True).strip()
        rollback_tag = "shuku-rollback:" + self.job["id"]
        self.command(["docker", "tag", old_image, rollback_tag])
        nonce = secrets.token_hex(32)
        snapshot = None
        changed = False
        self.maintenance_gate(nonce)
        try:
            snapshot = self.backup(keep_stopped=True)
            atomic_text(self.control / "status" / ("recovery-" + self.job["id"] + ".json"),
                json.dumps({"kind": "update", "backup": str(snapshot), "old_revision": old_revision,
                    "old_image": rollback_tag, "image_name": image_name}), 0o600)
            self.update_status("running", "网站已进入维护，正在构建并升级数据库；请勿重复提交")
            self.git("switch", "--detach", payload["sha"])
            changed = True
            # 使用更新后的部署入口，但不提前解除维护或对外宣布成功。
            self.command(["bash", str(self.root / "deploy.sh"), "maintenance-deploy"])
            self.smoke(nonce)
        except Exception:
            if snapshot and changed:
                self.recovery_required = True
                self.update_status("running", "升级未通过，正在恢复升级前版本和完整备份")
                self.restore_snapshot(snapshot, old_revision, rollback_tag, image_name)
                self.smoke(nonce)
                self.maintenance_gate(None)
                self.recovery_required = False
                self.update_status("rolled_back", "升级未成功，已恢复旧版本和升级前数据。可稍后重试；详细日志保留在服务器。")
                return
            self.recovery_required = True
            self.compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "360", "web", "caddy")
            self.maintenance_gate(None)
            self.recovery_required = False
            raise
        self.maintenance_gate(None)
        self.update_status("completed", "已更新到 " + payload["tag"] + "，网站检查通过，原有配置和图书数据已保留。")

    def run_one(self):
        self.heartbeat()
        active = self.control / "status" / "active.json"
        if active.exists():
            # 上一进程可能在迁移或恢复中中断，禁止自动重新执行未知阶段。
            previous = json.loads(active.read_text(encoding="utf-8"))
            self.job = previous
            status_file = self.control / "status" / ("job-" + previous["id"] + ".json")
            if status_file.exists():
                self.result = json.loads(status_file.read_text(encoding="utf-8"))
            self.update_status("failed", "上次维护被中断，已暂停新任务。请检查服务器日志和完整备份后人工恢复。")
            return
        pending = self.control / "requests" / "pending.json"
        try:
            fd = os.open(pending, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError("非法维护请求文件")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = None
                job = validate_job(json.loads(stream.read(16385)))
            atomic_text(active, json.dumps(job))
            pending.unlink()
        finally:
            if fd is not None:
                os.close(fd)
        self.job = job
        stop = threading.Event()
        def beat():
            while not stop.wait(10):
                self.heartbeat()
        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            self.update_status("running", "维护任务已开始")
            if job["kind"] == "domains":
                self.apply_domains(job["payload"])
            elif job["kind"] == "update":
                self.update_release(job["payload"])
            else:
                self.backup()
                self.update_status("completed", "备份已完成并校验，请将备份另存到安全位置。")
        except Exception as exc:
            # 网页仅显示友好信息；子进程输出（可能涉及配置）留在受限日志。
            message = str(exc) if isinstance(exc, ValueError) else "操作未完成，请查看服务器维护日志。若恢复也失败，网站将保持维护状态，勿重复升级。"
            self.update_status("failed", message)
            print(f"维护失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            stop.set()
            thread.join(timeout=2)
            if not self.recovery_required:
                active.unlink(missing_ok=True)
            self.heartbeat()

    def install(self):
        if not Path("/run/systemd/system").exists():
            print("未检测到 systemd：网站可运行；网页域名变更和升级需配置维护服务。")
            return
        unit = "shuku-maintenance-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        if any(c in str(self.root) for c in '\n\r"%\\'):
            raise ValueError("部署路径含维护服务不支持的字符")
        service = f'''[Unit]
Description=Shuku website maintenance
After=docker.service network-online.target
[Service]
Type=oneshot
WorkingDirectory="{self.root}"
ExecStart=/bin/bash "{self.root}/deploy.sh" maintenance
TimeoutStartSec=7200
UMask=0077
'''
        timer = f'''[Unit]
Description=Check administrator-confirmed maintenance requests
[Timer]
OnBootSec=30
OnUnitInactiveSec=15
Unit={unit}.service
[Install]
WantedBy=timers.target
'''
        atomic_text(Path("/etc/systemd/system") / (unit + ".service"), service)
        atomic_text(Path("/etc/systemd/system") / (unit + ".timer"), timer)
        self.command(["systemctl", "daemon-reload"])
        self.command(["systemctl", "enable", "--now", unit + ".timer"])
        self.heartbeat()
        print("网站维护服务已启用：" + unit)


def main():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("维护执行服务仅在 Linux 服务器上由 root 运行；网页不需要也不会获得 root 权限。")
    worker = HostMaintenance()
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if action == "prepare":
        worker.prepare()
    elif action == "install":
        worker.install()
    elif action == "run":
        worker.run_one()
    else:
        raise SystemExit("不支持的维护服务操作")


if __name__ == "__main__":
    main()
