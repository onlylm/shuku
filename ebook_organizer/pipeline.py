#!/usr/bin/env python3
# 全自动流水线：把「扫描好的书」一次性跑完 分类→版权申报→封面上R2→传网盘→同步网站。
# 设计要点：
#  - 断点续传：已 verified 的封面 / 已分享的网盘 / 已 ok 的网站同步会自动跳过（--force 可强制重做）。
#  - 闸门分流：未过「分类确认 + 版权确认 + 有来源」闸门的书不会中断流程，而是进 needs_classification / needs_rights 队列，最后在摘要里列出，供人工补。
#  - 安全：每本之间 control.check() 让桌面端可暂停/取消；dry_run 只统计不写库不联网。
#  - 复用现有 upload_cover / upload_book / quark_connector / SiteClient / engine.classify，不重复造轮子。
from __future__ import annotations

from pathlib import Path

from .engine import classify, DEFAULT_RULES
from .connections import upload_cover, upload_book, quark_connector, SiteClient
from .safeio import Control


def _cfg(ws, config, key):
    v = config.get(key) if isinstance(config, dict) else None
    return v if v else ws.setting(key)


def _qualified(ws, book):
    m = book["metadata"]
    return (
        m.get("rights_review_status") == "confirmed"
        and (m.get("source_reference") or "").strip()
        and m.get("copyright_status")
        and m.get("main_category")
        and m.get("classification_status") == "confirmed"
    )


def run_full_pipeline(ws, config, credentials, opts=None, control=None, progress=lambda _: None):
    control = control or Control()
    opts = opts or {}
    provider = opts.get("provider", "quark")
    dry = opts.get("dry_run", False)
    force = opts.get("force", False)
    auto_classify = opts.get("auto_classify", True)
    auto_rights = opts.get("auto_rights", True)
    rights_status = opts.get("rights_status", "authorized")
    source_reference = opts.get("source_reference", "自有/已购电子书资源")
    publish = opts.get("publish", False)
    batch = max(1, min(500, int(opts.get("batch", 20))))
    limit = int(opts.get("limit", 0))

    summary = {
        "total": 0, "classified_auto": 0, "needs_classification": [],
        "rights_auto": 0, "needs_rights": [],
        "cover_uploaded": 0, "cover_skipped_none": 0, "cover_verified_skip": 0, "cover_failed": 0,
        "book_uploaded": 0, "book_skipped_done": 0, "book_failed": 0,
        "site_synced": 0, "site_skipped": 0, "site_failed": 0,
        "errors": [],
    }

    all_books = ws.books()
    if opts.get("book_ids"):
        wanted = {x.strip() for x in str(opts["book_ids"]).split(",") if x.strip()}
        all_books = [b for b in all_books if b["book_id"] in wanted]
    scope = all_books if not limit else all_books[:limit]
    summary["total"] = len(scope)
    rules = ws.setting("category_rules", DEFAULT_RULES)

    # ---- 阶段 1：分类自动确认 ----
    progress("[1/5] 分类：对未确认分类的书做关键词自动确认…")
    for b in scope:
        control.check()
        bid, m = b["book_id"], b["metadata"]
        if m.get("classification_status") == "confirmed" and m.get("main_category"):
            continue
        if not auto_classify:
            summary["needs_classification"].append(bid)
            continue
        try:
            cand = classify(Path("/tmp/_pipeline.epub"), Path("/tmp"), m, rules)
        except Exception:
            cand = None
        cands = (cand or {}).get("classification_candidates") or []
        if cands:
            if not dry:
                ws.edit(bid, {
                    "main_category": cands[0]["name"], "subcategory": "",
                    "classification_status": "confirmed",
                })
            summary["classified_auto"] += 1
        else:
            summary["needs_classification"].append(bid)
    progress(f"  自动分类 {summary['classified_auto']} 本；待人工分类 {len(summary['needs_classification'])} 本")

    # ---- 阶段 2：版权批量申报 ----
    progress("[2/5] 版权：对未确认的书批量申报默认策略…")
    for b in scope:
        control.check()
        bid, m = b["book_id"], b["metadata"]
        if m.get("rights_review_status") == "confirmed" and (m.get("source_reference") or "").strip():
            continue
        if not auto_rights:
            summary["needs_rights"].append(bid)
            continue
        if not dry:
            ws.edit(bid, {
                "copyright_status": rights_status, "source_reference": source_reference,
                "rights_review_status": "confirmed",
            })
        summary["rights_auto"] += 1
    progress(f"  批量申报 {summary['rights_auto']} 本；待人工确认版权 {len(summary['needs_rights'])} 本")

    uploadable = [b for b in scope if _qualified(ws, b) and not b["excluded"] and b["status"] not in {"failed", "blocked"}]

    # ---- 阶段 3：封面上 R2 ----
    if dry:
        would = sum(1 for b in uploadable if b.get("cover_path") and (ws.result(b["book_id"], "r2") or {}).get("state") != "verified")
        summary["_cover_would"] = would
        progress(f"  [dry-run] 预计上传封面 {would} 本（有封面且尚未 verified）")
    else:
        progress("[3/5] 封面上 R2…")
        r2cfg = {k: _cfg(ws, config, k) for k in ("r2_account", "r2_bucket", "r2_public")}
        have_r2 = all(r2cfg.values()) and credentials.get("r2_key") and credentials.get("r2_secret")
        if not have_r2:
            progress("  未配置 R2（账户/桶/域名 或 AK/SK 缺失），跳过封面上传；补全后重跑即可。")
        cover_todo = [b for b in uploadable if b.get("cover_path") and (ws.result(b["book_id"], "r2") or {}).get("state") != "verified"]
        for b in cover_todo:
            control.check()
            bid = b["book_id"]
            try:
                upload_cover(ws, bid, r2cfg, credentials)
                summary["cover_uploaded"] += 1
                progress(f"  封面✓ {b['metadata'].get('title')}")
            except Exception as e:
                summary["cover_failed"] += 1
                summary["errors"].append(f"cover {bid}: {e}")
                progress(f"  封面✗ {bid}: {e}")

    # ---- 阶段 4：传网盘 ----
    progress(f"[4/5] 传网盘（{provider}）…")
    connector = None
    if not dry and provider == "quark":
        try:
            connector = quark_connector(ws, config)
        except Exception as e:
            progress(f"  夸克运行环境检查未通过，中止网盘上传：{e}")
            summary["errors"].append("quark gate: " + str(e))
            connector = None
    quark_todo = [b for b in uploadable if not (ws.result(b["book_id"], provider) or {}).get("share_url")]
    if dry:
        summary["_book_would"] = len(quark_todo)
        progress(f"  [dry-run] 预计传网盘 {len(quark_todo)} 本")
    elif connector is not None:
        for b in quark_todo:
            control.check()
            bid = b["book_id"]
            try:
                upload_book(ws, bid, provider, config, credentials, control, connector=connector)
                summary["book_uploaded"] += 1
                progress(f"  网盘✓ {b['metadata'].get('title')}")
            except Exception as e:
                summary["book_failed"] += 1
                summary["errors"].append(f"book {bid}: {e}")
                progress(f"  网盘✗ {bid}: {e}")
    else:
        summary["book_skipped_done"] = len(quark_todo)
        progress(f"  网盘上传跳过（环境闸门未过或 dry-run），{len(quark_todo)} 本待处理")

    # ---- 阶段 5：同步网站 ----
    progress("[5/5] 同步网站…")
    site_todo = [b["book_id"] for b in scope
                 if (ws.result(b["book_id"], provider) or {}).get("share_url")
                 and (force or (ws.result(b["book_id"], "site:" + _cfg(ws, config, "site_id")) or {}).get("status") != "ok")]
    if dry:
        summary["_site_would"] = len(site_todo)
        progress(f"  [dry-run] 预计同步网站 {len(site_todo)} 本")
    elif not site_todo:
        progress("  没有需要同步的书。")
    else:
        client = SiteClient(ws, config, credentials)
        try:
            info = client.info()
            progress(f"  网站连接正常 site_id={info.get('site_id')} 分类 {len(info.get('categories', []))}")
            for i in range(0, len(site_todo), batch):
                chunk = site_todo[i:i + batch]
                try:
                    preview = client.preview(chunk)
                except Exception as e:
                    summary["site_failed"] += len(chunk)
                    summary["errors"].append(f"preview: {e}")
                    progress(f"  预检✗: {e}")
                    continue
                rows = {r["book_id"]: r for r in preview["rows"]}
                choices = []
                for bid in chunk:
                    row = rows.get(bid)
                    if not row or row.get("error"):
                        summary["site_skipped"] += 1
                        progress(f"  跳过 {bid}: {row.get('error') if row else '不在预检'}")
                        continue
                    ch = {"book_id": bid, "action": row["action"], "publish": publish}
                    if row["action"] in ("choose", "bind") and row.get("candidates"):
                        ch["resource_id"] = row["candidates"][0]["id"]
                    choices.append(ch)
                if not choices:
                    continue
                try:
                    client.commit(preview, choices, control, progress)
                    summary["site_synced"] += len(choices)
                    progress(f"  批次 {i // batch + 1} 提交 {len(choices)} 本")
                except Exception as e:
                    summary["site_failed"] += len(choices)
                    summary["errors"].append(f"commit: {e}")
                    progress(f"  提交✗: {e}")
        finally:
            client.close()
    progress("流水线结束。" + ("（dry-run，未做真实上传/写入）" if dry else ""))
    return summary


def format_summary(summary):
    lines = []
    lines.append(f"总处理：{summary['total']} 本")
    lines.append(f"分类自动确认：{summary['classified_auto']} 本｜待人工分类：{len(summary['needs_classification'])} 本")
    lines.append(f"版权批量申报：{summary['rights_auto']} 本｜待人工确认：{len(summary['needs_rights'])} 本")
    lines.append(f"封面：上传 {summary['cover_uploaded']}｜跳过(无/已验证) {summary['cover_skipped_none']+summary['cover_verified_skip']}｜失败 {summary['cover_failed']}"
                 + (f"｜[dry]预计 {summary.get('_cover_would',0)}" if '_cover_would' in summary else ""))
    lines.append(f"网盘：上传 {summary['book_uploaded']}｜跳过 {summary['book_skipped_done']}｜失败 {summary['book_failed']}"
                 + (f"｜[dry]预计 {summary.get('_book_would',0)}" if '_book_would' in summary else ""))
    lines.append(f"网站：同步 {summary['site_synced']}｜跳过 {summary['site_skipped']}｜失败 {summary['site_failed']}"
                 + (f"｜[dry]预计 {summary.get('_site_would',0)}" if '_site_would' in summary else ""))
    if summary["errors"]:
        lines.append(f"错误 {len(summary['errors'])} 条（首条：{summary['errors'][0]}）")
    return "\n".join(lines)
