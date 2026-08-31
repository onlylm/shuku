#!/usr/bin/env python3
# 把「已传到夸克并生成分享链接」的书批量同步到网站（静页书房）。
# 复用 connections.SiteClient：preview(ids) -> commit(choices)。每批预览+提交，失败只记 events，可重跑续传。
#
# 前置：先跑 setup_site_sync.py 接好 site_url / site_token / site_id。
# 用法：
#   .venv\Scripts\python.exe scripts\site_sync_batch.py --limit 1 --publish     # 先测 1 本并请求发布
#   .venv\Scripts\python.exe scripts\site_sync_batch.py --publish               # 全量（已同步过的自动跳过）
#   .venv\Scripts\python.exe scripts\site_sync_batch.py --dry-run               # 只统计待同步本数
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace
from ebook_organizer.connections import Credentials, SiteClient

WS_PATH = Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(WS_PATH))
    ap.add_argument("--limit", type=int, default=0, help="0=全量；>0 限制本次本数")
    ap.add_argument("--batch", type=int, default=20, help="每批预览/提交本数(1~500)")
    ap.add_argument("--publish", action="store_true", help="同步时请求发布（链接校验通过才发布）")
    ap.add_argument("--force", action="store_true", help="忽略已同步标记，强制重新提交（用于补封面/更新元数据）")
    ap.add_argument("--book-ids", default="", help="逗号分隔的 book_id 列表，仅同步这些书（用于补传指定书封面）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不实际同步")
    args = ap.parse_args()

    ws = Workspace(Path(args.workspace))
    wid = ws.setting("workspace_id")
    site_url = ws.setting("site_url")
    site_id = ws.setting("site_id")
    if not site_url or not site_id:
        print("缺少 site_url / site_id，请先运行 setup_site_sync.py")
        return
    creds = Credentials(wid)
    token = creds.get("site_token")
    if not token:
        print("缺少 site_token（Windows 凭据库），请先运行 setup_site_sync.py")
        return
    config = {"site_url": site_url, "site_id": site_id}

    def has_quark(bid):
        return (ws.result(bid, "quark") or {}).get("share_url")

    def already_synced(bid):
        return (ws.result(bid, "site:" + site_id) or {}).get("status") == "ok"

    books = ws.books()
    todo = [b["book_id"] for b in books if has_quark(b["book_id"]) and (args.force or not already_synced(b["book_id"]))]
    if args.book_ids:
        ids = {x.strip() for x in args.book_ids.split(",") if x.strip()}
        todo = [b for b in todo if b in ids]
    print(f"已传夸克且待同步到网站: {len(todo)} 本" + ("（--force 含已同步）" if args.force else ""))
    if args.limit:
        todo = todo[:args.limit]

    if args.dry_run:
        print("（dry-run 结束，未做任何同步）")
        return

    client = SiteClient(ws, config, creds)
    try:
        info = client.info()
        print(f"网站连接正常，site_id={info.get('site_id')}，分类 {len(info.get('categories', []))} 个")
    except Exception as e:
        print("网站连接失败：", e)
        return

    ok = fail = skip = 0
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        try:
            preview = client.preview(chunk)
        except Exception as e:
            print(f"[批次 {i // args.batch + 1}] preview 失败: {e}")
            fail += len(chunk)
            continue
        rows = {r["book_id"]: r for r in preview["rows"]}
        choices = []
        for bid in chunk:
            row = rows.get(bid)
            if not row or row.get("error"):
                print(f"  跳过 {bid}: {row.get('error') if row else '不在预检中'}")
                skip += 1
                continue
            choice = {"book_id": bid, "action": row["action"], "publish": args.publish}
            if row["action"] in ("choose", "bind") and row.get("candidates"):
                choice["resource_id"] = row["candidates"][0]["id"]
            choices.append(choice)
        if not choices:
            continue
        try:
            client.commit(preview, choices)
            ok += len(choices)
            print(f"[批次 {i // args.batch + 1}] 已提交 {len(choices)} 本")
        except Exception as e:
            print(f"[批次 {i // args.batch + 1}] commit 失败: {e}")
            fail += len(choices)
        time.sleep(0.2)

    print(f"完成: 同步成功 {ok} / 失败 {fail} / 跳过 {skip}（失败记录在 events，修正后重跑可续传）")


if __name__ == "__main__":
    main()
