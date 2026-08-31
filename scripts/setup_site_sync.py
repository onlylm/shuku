#!/usr/bin/env python3
# 一次性把「桌面整理软件 -> 网站」的同步授权接好：
#   1) 在网站 local.db 的 organizer_tokens 里新建一条令牌；
#   2) 探测 8000/8001 哪个端口的 /api/v1/organizer/info 可用，取 site_id；
#   3) site_url / site_id 写入工作区 settings，site_token 写入 Windows 凭据库。
# 运行：.venv\Scripts\python.exe scripts\setup_site_sync.py
import argparse
import hashlib
import secrets
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace
from ebook_organizer.connections import Credentials

WS_PATH = Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace"
DB = ROOT / "local.db"
PORTS = [8000, 8001]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(WS_PATH))
    ap.add_argument("--label", default="WorkBuddy批量同步")
    ap.add_argument("--port", type=int, default=0, help="0=自动探测可用端口")
    args = ap.parse_args()

    ws = Workspace(Path(args.workspace))
    wid = ws.setting("workspace_id")

    # 1) 新建令牌（同 label 的旧令牌先停用）
    db = sqlite3.connect(DB, timeout=30)
    db.execute("UPDATE organizer_tokens SET is_active=0 WHERE label=?", (args.label,))
    token = "eo_" + secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.execute(
        "INSERT INTO organizer_tokens(admin_user_id,label,token_hash,is_active,created_at,updated_at) "
        "VALUES(1,?,?,1,datetime('now'),datetime('now'))",
        (args.label, token_hash),
    )
    db.commit()
    print("已创建同步令牌:", args.label)

    # 2) 探测可用端口
    port = args.port
    if not port:
        for p in PORTS:
            try:
                r = httpx.get(f"http://127.0.0.1:{p}/api/v1/organizer/info",
                              headers={"Authorization": "Bearer " + token}, timeout=5)
                if r.status_code == 200 and r.json().get("site_id"):
                    port = p
                    break
            except Exception:
                pass
    if not port:
        print("找不到可用端口：8000/8001 都没有响应 /api/v1/organizer/info（网站可能未起或令牌无效）")
        return
    site_url = f"http://127.0.0.1:{port}"
    info = httpx.get(site_url + "/api/v1/organizer/info",
                     headers={"Authorization": "Bearer " + token}, timeout=10).json()
    site_id = info["site_id"]
    print(f"网站 site_id: {site_id} | 分类数: {len(info.get('categories', []))}")

    # 3) 写入配置
    creds = Credentials(wid)
    creds.set("site_token", token)          # Windows 凭据库，不进 settings
    ws.set_setting("site_url", site_url)
    ws.set_setting("site_id", site_id)
    print(f"已写入：site_url={site_url} | site_id={site_id} | site_token->Windows 凭据库")
    print("分类示例:", [c["name"] for c in info.get("categories", [])][:12])


if __name__ == "__main__":
    main()
