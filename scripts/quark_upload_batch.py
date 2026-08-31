#!/usr/bin/env python3
# 批量把工作区里「已确认版权 + 已确认分类」且源文件仍存在的书，
# 通过官方夸克连接器上传到指定目录，并生成分享链接（复用 connections.upload_book）。
#
# 前提：当前运行环境须被夸克识别（resolve-agent 返回 QK_AGENT_ID）。
#       在 WorkBuddy 代理环境中可直接走批量，无需逐本手工回填。
#
# 用法（项目 venv）：
#   .venv\Scripts\python.exe scripts\quark_upload_batch.py --dry-run                  # 只统计，不上传
#   .venv\Scripts\python.exe scripts\quark_upload_batch.py --parent <目录fid> --limit 1  # 先试 1 本
#   .venv\Scripts\python.exe scripts\quark_upload_batch.py --parent <目录fid>          # 全量（断点续传）
#
# 说明：已生成 share_url 的书自动跳过；失败仅记到 events，不影响其他书；
#       同一连接器实例在整批中复用，避免每本重复做环境校验。
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace
from ebook_organizer.connections import Credentials, quark_connector, upload_book

WS_PATH = Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace"
CLI = r"D:\网盘拉新\runtime\cloud-connectors\quark-1.0.15\scripts\quark-drive.cjs"


def qualifies(book):
    """upload_book 内部要求的版权与分类前置条件。"""
    meta = book["metadata"]
    if meta.get("rights_review_status") != "confirmed":
        return False
    if not meta.get("source_reference") or not meta.get("copyright_status"):
        return False
    if not meta.get("main_category") or meta.get("classification_status") != "confirmed":
        return False
    return True


def has_source(ws, book):
    try:
        ws.source(book["book_id"])
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(WS_PATH))
    ap.add_argument("--parent", default=None, help="夸克目标目录 fid；确认选择根目录时填 0。也可在工作区设置 quark_parent")
    ap.add_argument("--limit", type=int, default=0, help="0=全量；>0 限制本次处理本数")
    ap.add_argument("--dry-run", action="store_true", help="只统计与校验环境，不实际上传")
    args = ap.parse_args()

    ws = Workspace(Path(args.workspace))
    wid = ws.setting("workspace_id")
    parent = (args.parent or "").strip() or ws.setting("quark_parent")
    config = {"quark_cli": CLI}
    if parent:
        config["quark_parent"] = parent.strip()
    creds = Credentials(wid)

    books = ws.books()
    with_source = [b for b in books if has_source(ws, b)]
    qualified = [b for b in with_source if qualifies(b)]
    need_review = [b for b in with_source if not qualifies(b)]
    no_source = len(books) - len(with_source)
    # 已排除/异常的书无法上传，单列跳过（不要再算进待上传去撞错）
    blocked_ids = {b["book_id"] for b in qualified if b["status"] in {"failed", "blocked"} or b["excluded"]}
    qualified = [b for b in qualified if b["book_id"] not in blocked_ids]

    def shared(b):
        return (ws.result(b["book_id"], "quark") or {}).get("share_url")

    already = [b for b in qualified if shared(b)]
    todo = [b for b in qualified if not shared(b)]

    print(f"总书: {len(books)}")
    print(f"  有源文件:   {len(with_source)}")
    print(f"  缺源文件:   {no_source}（无法上传）")
    print(f"  版权+分类已确认: {len(qualified) + len(blocked_ids)}")
    print(f"  已排除/异常(不可上传): {len(blocked_ids)}")
    print(f"  已生成分享链接(跳过): {len(already)}")
    print(f"  待上传:     {len(todo)}")
    print(f"  需补审(版权/分类未确认): {len(need_review)}")

    # 环境校验（只读，不伪造环境）
    try:
        quark_connector(ws, config)
        print("夸克环境校验: 通过（当前运行环境被官方连接器识别）")
    except Exception as e:
        print("夸克环境校验: 未通过 —— 已中止，不会伪造环境绕过限制。")
        print("  原因:", e)
        return

    if args.dry_run:
        print("（dry-run 结束，未做任何上传）")
        return

    if not parent:
        print("缺少夸克目标目录编号（--parent 或 工作区设置 quark_parent）。确认选择根目录时填 0。")
        return

    connector = quark_connector(ws, config)
    if args.limit:
        todo = todo[:args.limit]

    ok = fail = need_check = 0
    for i, b in enumerate(todo, 1):
        bid = b["book_id"]
        try:
            out = upload_book(ws, bid, "quark", config, creds, connector=connector)
            print(f"[{i}/{len(todo)}] OK   {bid} -> {out.get('share_url')}")
            ok += 1
        except Exception as e:
            msg = str(e)
            print(f"[{i}/{len(todo)}] FAIL {bid}: {msg}")
            ws.event("quark_batch", f"upload failed: {msg}", bid)
            # 已上传但分享未生成等情况，提示到网盘核对，不计入硬失败
            if any(k in msg for k in ("核对", "重复", "分享尚未生成", "未确认")):
                need_check += 1
            else:
                fail += 1
        time.sleep(0.2)

    print(f"完成: 成功 {ok} / 失败 {fail} / 需到网盘人工核对 {need_check}（失败记录在 events，修正后重跑可续传）")


if __name__ == "__main__":
    main()
