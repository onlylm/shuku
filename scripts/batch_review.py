#!/usr/bin/env python3
# 统一批量申报：对全部书补齐合规字段，并把「建议分类(suggested)」提升为「已确认」。
# 所有改动经 workspace.edit 写入，可逐本撤销（ws.undo(book_id)）。
# 默认只预览；加 --apply 才真正写入（写入后仍可用脚本 quark_upload_batch.py 上传）。
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace

WS_PATH = Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace"


def qualifies(meta):
    return (
        meta.get("rights_review_status") == "confirmed"
        and (meta.get("source_reference") or "").strip()
        and (meta.get("copyright_status") or "").strip()
        and (meta.get("main_category") or "").strip()
        and meta.get("classification_status") == "confirmed"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=str(WS_PATH))
    ap.add_argument("--copyright-status", default="authorized", help="统一申报的版权状态：authorized/public_domain/open_license")
    ap.add_argument("--source-reference", default="自有/已购电子书资源", help="统一申报的来源说明")
    ap.add_argument("--apply", action="store_true", help="默认只预览；加此参数才真正写入（可撤销）")
    args = ap.parse_args()

    ws = Workspace(Path(args.workspace))
    books = ws.books()

    planned = []
    for b in books:
        m = b["metadata"]
        changes = {}
        if m.get("rights_review_status") != "confirmed":
            changes["rights_review_status"] = "confirmed"
        if not (m.get("copyright_status") or "").strip():
            changes["copyright_status"] = args.copyright_status
        if not (m.get("source_reference") or "").strip():
            changes["source_reference"] = args.source_reference
        if m.get("classification_status") == "suggested":
            cands = m.get("classification_candidates") or []
            if cands:
                changes["main_category"] = cands[0]["name"]
                changes["classification_status"] = "confirmed"
        if changes:
            planned.append((b["book_id"], changes))

    print(f"总书: {len(books)} | 本次计划改动: {len(planned)} 本")
    types = Counter()
    for _, ch in planned:
        for k in ch:
            types[k] += 1
    print("改动字段计数:", dict(types))
    before = sum(1 for b in books if qualifies(b["metadata"]))
    print(f"写入前「可上传(合规+分类齐全)」本数: {before}")

    if not args.apply:
        print("\n[预览] 前 10 本示例（不写入）：")
        for bid, ch in planned[:10]:
            print(f"  {bid}: {json.dumps(ch, ensure_ascii=False)}")
        print("\n加上 --apply 才会真正写入（每本记录旧值，可 ws.undo(book_id) 撤销）。")
        return

    done = 0
    for bid, ch in planned:
        try:
            ws.edit(bid, ch)
            done += 1
        except Exception as e:
            print(f"FAIL {bid}: {e}")
    print(f"已写入: {done}/{len(planned)}")

    after_books = ws.books()
    after = sum(1 for b in after_books if qualifies(b["metadata"]))
    print(f"写入后「可上传(合规+分类齐全)」本数: {after}（其余为 pending 无分类，需补分类后上传）")


if __name__ == "__main__":
    main()
