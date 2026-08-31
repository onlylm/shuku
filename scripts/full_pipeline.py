#!/usr/bin/env python3
# 全自动流水线（无头版）：对扫描好的全部书跑完 分类→版权申报→封面上R2→传网盘→同步网站。
# 用法：
#   .venv\Scripts\python.exe scripts\full_pipeline.py --dry-run            # 只统计，不写库不联网
#   .venv\Scripts\python.exe scripts\full_pipeline.py --limit 2 --publish   # 先拿 2 本端到端冒烟
#   .venv\Scripts\python.exe scripts\full_pipeline.py --publish            # 全量（断点续传）
# 前置：先跑 setup_site_sync.py 接好 site_token；R2 凭据用 set_r2_credentials.py 配置。
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ebook_organizer.workspace import Workspace
from ebook_organizer.connections import Credentials
from ebook_organizer.pipeline import run_full_pipeline, format_summary
from ebook_organizer.safeio import Control


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--provider", default="quark", choices=["quark", "baidu"])
    ap.add_argument("--publish", action="store_true", help="同步时请求发布（链接校验通过才发布）")
    ap.add_argument("--limit", type=int, default=0, help="0=全部；>0 只处理前 N 本做冒烟")
    ap.add_argument("--batch", type=int, default=20, help="网站每批同步本数(1~500)")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库不联网")
    ap.add_argument("--rights-status", default="authorized", choices=["authorized", "public_domain", "open_license"])
    ap.add_argument("--source-reference", default="", help="批量确认版权时必须填写已核对的真实来源")
    ap.add_argument("--auto-classify", action="store_true", help="生成关键词分类建议，仍须人工确认后上传")
    ap.add_argument("--confirm-rights", dest="auto_rights", action="store_true", help="明确确认本批版权类别和来源")
    ap.add_argument("--no-auto-classify", dest="auto_classify", action="store_false", help="不自动确认分类，未分类书进待处理队列")
    ap.add_argument("--no-rights-default", dest="auto_rights", action="store_false", help="不批量申报版权，未确认书进待处理队列")
    ap.set_defaults(auto_classify=False, auto_rights=False)
    ap.add_argument("--force", action="store_true", help="重新校验封面及同步网站，不重复上传已有分享的文件")
    ap.add_argument("--overwrite", action="store_true", help="明确覆盖网站非空资料（默认仅补空）")
    ap.add_argument("--book-ids", default=None, help="逗号分隔的 book_id 列表；显式空值不处理任何书")
    args = ap.parse_args()

    ws_path = Path(args.workspace) if args.workspace else (Path.home() / "AppData" / "Local" / "EbookOrganizer" / "workspace")
    ws = Workspace(ws_path)
    wid = ws.setting("workspace_id")

    config = {k: ws.setting(k) for k in ("site_url", "site_id", "r2_account", "r2_bucket", "r2_public", "quark_parent", "quark_cli", "connector_runtime", "baidu_root")}
    config.update(ws.setting("connections", {}))
    creds = Credentials(wid)
    if not args.dry_run and not creds.get("site_token"):
        print("缺少 site_token（Windows 凭据库）。请先运行：.venv\\Scripts\\python.exe scripts\\setup_site_sync.py")
        return

    opts = dict(provider=args.provider, publish=args.publish, limit=args.limit, batch=args.batch,
                dry_run=args.dry_run, rights_status=args.rights_status, source_reference=args.source_reference,
                auto_classify=args.auto_classify, auto_rights=args.auto_rights, force=args.force, overwrite=args.overwrite,
                book_ids=args.book_ids)

    print("全自动流水线开始" + ("（dry-run）" if args.dry_run else ""))
    summary = run_full_pipeline(ws, config, creds, opts, control=Control(), progress=print)
    print("-" * 40)
    print(format_summary(summary))
    if summary["needs_classification"]:
        print(f"\n待人工分类（前 20）：{summary['needs_classification'][:20]}")
    if summary["needs_rights"]:
        print(f"\n待人工确认版权（前 20）：{summary['needs_rights'][:20]}")


if __name__ == "__main__":
    main()
