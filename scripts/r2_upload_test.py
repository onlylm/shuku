#!/usr/bin/env python3
# 无头测试：用桌面整理软件已有的 upload_cover 上传一本封面到 Cloudflare R2。
# 用法（项目 venv）：
#   D:\网盘拉新\.venv\Scripts\python.exe D:\网盘拉新\scripts\r2_upload_test.py --dry-run
#   D:\网盘拉新\.venv\Scripts\python.exe D:\网盘拉新\scripts\r2_upload_test.py --confirm
#   D:\网盘拉新\.venv\Scripts\python.exe D:\网盘拉新\scripts\r2_upload_test.py --book-id BK_xxx --confirm
# 默认取工作区里第一本“有封面”的书；--dry-run 只校验配置不写 R2；--confirm 才真正 PUT。
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace
from ebook_organizer.connections import Credentials, upload_cover


def _mask(v):
    if not isinstance(v, str) or len(v) <= 6:
        return v
    return v[:4] + "…" + v[-2:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--book-id", default=None, help="指定书；不填取第一本有封面的")
    ap.add_argument("--dry-run", action="store_true", help="只校验配置，不写 R2")
    ap.add_argument("--confirm", action="store_true", help="真正 PUT 到 R2 并做公开校验")
    args = ap.parse_args()

    ws_path = Path(args.workspace) if args.workspace else (Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace")
    ws = Workspace(ws_path)
    wid = ws.setting("workspace_id")
    config = {k: ws.setting(k) for k in ("r2_account", "r2_bucket", "r2_public")}

    bid = args.book_id
    if not bid:
        bid = next((b["book_id"] for b in ws.books() if b.get("cover_path")), None)
    if not bid:
        print("没有可上传封面的书，请先扫描并提取封面。")
        return
    book = ws.book(bid)
    print(f"工作区: {ws.root}")
    print(f"目标书: {bid} | 封面版本: {book.get('cover_version')} | 封面: {book.get('cover_path')}")
    print(f"R2 配置: account={_mask(config['r2_account'])} bucket={config['r2_bucket']} public={_mask(config['r2_public'])}")

    if not all(config.values()):
        print("缺少 R2 非密配置，请先运行 set_r2_credentials.py 或在桌面软件填写。")
        return

    if args.dry_run or not args.confirm:
        print("[dry-run] 配置齐全即停止；用 --confirm 才真正上传到 R2。")
        return

    creds = Credentials(wid)
    if not creds.get("r2_key") or not creds.get("r2_secret"):
        print("未读取到 R2 AK/SK（Windows 凭据库）。请先运行 set_r2_credentials.py 或在桌面软件填写同步授权处的 R2 凭据。")
        return

    state = upload_cover(ws, bid, config, creds)
    print("上传结果:", state)


if __name__ == "__main__":
    main()
