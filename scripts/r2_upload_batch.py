#!/usr/bin/env python3
# 批量把工作区里有封面的书传到 Cloudflare R2（复用 connections.upload_cover）。
# 用法（项目 venv）：
#   D:\网盘拉新\.venv\Scripts\python.exe D:\网盘拉新\scripts\r2_upload_batch.py --limit 5   # 先小批试 5 本
#   D:\网盘拉新\.venv\Scripts\python.exe D:\网盘拉新\scripts\r2_upload_batch.py             # 全量（可断点续传）
# 说明：state 已 verified 的书自动跳过；失败记录到 workspace events，不影响其他书。
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace
from ebook_organizer.connections import Credentials, upload_cover


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--limit", type=int, default=0, help="0=全量；>0 限制本次处理本数")
    ap.add_argument("--book-ids", default="", help="逗号分隔的 book_id 列表，仅上传这些书的封面（用于补传指定书）")
    args = ap.parse_args()

    ws_path = Path(args.workspace) if args.workspace else (Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace")
    ws = Workspace(ws_path)
    wid = ws.setting("workspace_id")
    config = {k: ws.setting(k) for k in ("r2_account", "r2_bucket", "r2_public")}
    if not all(config.values()):
        print("缺少 R2 非密配置，请先运行 set_r2_credentials.py 或在桌面软件填写。")
        return
    creds = Credentials(wid)
    if not creds.get("r2_key") or not creds.get("r2_secret"):
        print("缺少 R2 AK/SK（Windows 凭据库）。请先配置。")
        return

    books = [b for b in ws.books() if b.get("cover_path")]
    todo = [b for b in books if (ws.result(b["book_id"], "r2") or {}).get("state") != "verified"]
    if args.book_ids:
        ids = {x.strip() for x in args.book_ids.split(",") if x.strip()}
        todo = [b for b in todo if b["book_id"] in ids]
    print(f"有封面书: {len(books)} | 已验证跳过: {len(books) - len(todo)} | 本次待上传: {len(todo)}")
    if args.limit:
        todo = todo[:args.limit]

    ok = fail = 0
    for i, b in enumerate(todo, 1):
        bid = b["book_id"]
        try:
            st = upload_cover(ws, bid, config, creds)
            print(f"[{i}/{len(todo)}] OK   {bid} -> {st.get('url')}")
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(todo)}] FAIL {bid}: {e}")
            ws.event("r2_batch", f"upload failed: {e}", bid)
            fail += 1
        time.sleep(0.1)

    print(f"完成: 成功 {ok} / 失败 {fail}（失败可在 events 查看，修正后可重跑本脚本续传）")


if __name__ == "__main__":
    main()
