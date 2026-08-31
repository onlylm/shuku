#!/usr/bin/env python3
# 本地运行：配置桌面整理软件的 R2 封面上传凭据。
# 用法（用项目 venv，避免编码/依赖问题）：
#   .venv\Scripts\python.exe scripts\set_r2_credentials.py
# 说明：非密配置(r2_account/r2_bucket/r2_public)写入工作区 settings；
#       AK/SK 通过 getpass 在本机终端输入，只写入 Windows 凭据库(EbookOrganizer/<workspace_id>)，
#       不进聊天、不写进 settings 表、不落明文文件。
import getpass
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace
from ebook_organizer.connections import Credentials


def _read_secret(prompt):
    try:
        return getpass.getpass(prompt).strip()
    except Exception:
        return input(prompt).strip()


def main():
    raw = input("工作区路径(留空=默认 %LOCALAPPDATA%/EbookOrganizer/workspace): ").strip()
    ws_path = Path(raw) if raw else (Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace")
    ws = Workspace(ws_path)
    wid = ws.setting("workspace_id")
    print(f"工作区: {ws.root}")
    print(f"workspace_id: {wid}")

    account = input("R2 账户编号(32位十六进制): ").strip()
    bucket = input("R2 存储桶名称: ").strip()
    public = input("公开图片域名(https:// 开头，例如 https://img.example.com): ").strip()

    # 提前校验，避免保存错误格式
    if not re.fullmatch(r"[a-fA-F0-9]{32}", account):
        print("错误：R2 账户编号必须是 32 位十六进制（0-9/a-f/A-F），不是 cfat_xxx 那种令牌。")
        print("获取位置：Cloudflare 控制台右上角账户名旁，或浏览器地址 dash.cloudflare.com/<32hex>/...")
        raise SystemExit(1)
    if not bucket:
        print("错误：R2 存储桶名称不能为空。")
        raise SystemExit(1)
    parsed = urlsplit(public)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        print("错误：公开图片域名必须是 https:// 开头的干净域名，不能带账号/密码/查询参数/#锚点。")
        raise SystemExit(1)
    if parsed.path and parsed.path != "/":
        print("错误：公开图片域名不应带路径，请只填根域名（例如 https://img.example.com）。")
        raise SystemExit(1)

    ws.set_setting("r2_account", account)
    ws.set_setting("r2_bucket", bucket)
    ws.set_setting("r2_public", public)

    creds = Credentials(wid)
    key = _read_secret("R2 Access Key ID: ")
    secret = _read_secret("R2 Secret Access Key: ")
    ok_key = creds.set("r2_key", key)
    ok_secret = creds.set("r2_secret", secret)

    print("-" * 40)
    print(f"非密配置已写入 settings: account={account} bucket={bucket} public={public}")
    if ok_key and ok_secret:
        print(f"AK/SK 已写入 Windows 凭据库 (服务名 EbookOrganizer/{wid})")
    else:
        print("警告：Windows 凭据库不可用，AK/SK 仅保留在本次进程内存，请改用桌面软件界面填写。")
    print("配置完成。之后可运行 r2_upload_test.py 做单本测试。")


if __name__ == "__main__":
    main()
