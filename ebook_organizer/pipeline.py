"""桌面上传流水线：明确范围、逐项结果、可恢复任务；不改变夸克环境处理。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .engine import classify, DEFAULT_RULES, public_record
from .connections import upload_cover, upload_book, quark_connector, SiteClient, cover_is_current, sync_fingerprint
from .safeio import Cancelled, Control
from .batch_edit import error_message


def _cfg(ws, config, key):
    if key in config:
        return config[key]
    saved = ws.setting("connections", {})
    return saved[key] if key in saved else ws.setting(key)


def _qualified(ws, book):
    m = book["metadata"]
    return (
        m.get("rights_review_status") == "confirmed"
        and (m.get("source_reference") or "").strip()
        and m.get("copyright_status") in {"authorized", "public_domain", "open_license"}
        and m.get("main_category")
        and m.get("classification_status") == "confirmed"
    )


def _ids(value):
    if isinstance(value, str):
        value = value.split(",")
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def run_full_pipeline(ws, config, credentials, opts=None, control=None, progress=lambda _: None):
    control = control or Control()
    opts = dict(opts or {})
    config = {key: _cfg(ws, config or {}, key) for key in (
        "site_url", "site_id", "r2_account", "r2_bucket", "r2_public",
        "quark_parent", "quark_cli", "connector_runtime", "baidu_root")}
    config = {key: value for key, value in config.items() if value is not None}
    provider = opts.get("provider", "quark")
    if provider not in {"quark", "baidu"}:
        raise ValueError("暂不支持此网盘")
    limit = int(opts.get("limit", 0))
    if limit < 0:
        raise ValueError("本数限制不能为负数")
    if opts.get("auto_rights") and (
        opts.get("rights_status") not in {"authorized", "public_domain", "open_license"}
        or not str(opts.get("source_reference") or "").strip()
    ):
        raise ValueError("批量版权确认需要明确选择版权类别并填写真实来源说明")

    all_books = ws.books()
    # None 表示明确运行全书；空列表/空字符串绝不能退回整个书库。
    selected = opts.get("book_ids")
    if selected is not None:
        wanted = set(_ids(selected))
        all_books = [book for book in all_books if book["book_id"] in wanted]
    scope = all_books[:limit] if limit else all_books
    summary = {
        "total": len(scope), "classified_auto": 0, "needs_classification": [],
        "rights_auto": 0, "needs_rights": [], "needs_binding": [], "excluded_skipped": 0,
        "cover_uploaded": 0, "cover_skipped_none": 0, "cover_verified_skip": 0, "cover_failed": 0,
        "book_uploaded": 0, "book_skipped_done": 0, "book_failed": 0,
        "site_synced": 0, "site_skipped": 0, "site_failed": 0,
        "site_published": 0, "site_unpublished": 0, "errors": [],
    }
    if not scope:
        progress("没有选中的可处理图书；未写入、未上传。")
        return summary
    control.check()
    dry = bool(opts.get("dry_run"))
    saved_opts = {**opts, "book_ids": [b["book_id"] for b in scope]}
    job = None
    if not dry:
        ws.set_setting("last_pipeline_task", saved_opts)
        job = ws.start_job("pipeline", {"options": saved_opts, "summary": summary})

    def checkpoint():
        if job:
            ws.finish_job(job, "running", {"options": saved_opts, "summary": summary})

    try:
        _process(ws, config, credentials, opts, deepcopy(scope), control, progress, summary, checkpoint)
        if job:
            status = "failed" if summary["errors"] else (
                "needs_review" if any(summary[key] for key in ("needs_classification", "needs_rights", "needs_binding", "site_unpublished")) else "succeeded")
            ws.finish_job(job, status, {"options": saved_opts, "summary": summary})
    except Exception as exc:
        if job:
            ws.finish_job(job, "cancelled" if isinstance(exc, Cancelled) else "failed",
                          {"options": saved_opts, "summary": summary, "error": type(exc).__name__})
        raise
    progress("流水线结束。" + ("（预演：未写入、未联网）" if dry else "请核对逐项结果，上传不等于已发布。"))
    return summary


def _process(ws, config, credentials, opts, scope, control, progress, summary, checkpoint):
    dry, force = bool(opts.get("dry_run")), bool(opts.get("force"))
    provider = opts.get("provider", "quark")
    publish, overwrite = bool(opts.get("publish")), bool(opts.get("overwrite"))
    batch_size = max(1, min(500, int(opts.get("batch", 20))))
    rules = ws.setting("category_rules", DEFAULT_RULES)

    active = []
    for book in scope:
        if book["excluded"] or book["status"] in {"failed", "blocked"}:
            summary["excluded_skipped"] += 1
        else:
            active.append(book)

    progress("[1/5] 核对分类…")
    for book in active:
        control.check()
        bid, m = book["book_id"], book["metadata"]
        if m.get("classification_status") == "confirmed" and m.get("main_category"):
            continue
        candidates = m.get("classification_candidates") or []
        if opts.get("auto_classify") and not candidates:
            candidates = classify(Path("pipeline.epub"), Path("."), m, rules).get("classification_candidates", [])
        if opts.get("auto_classify") and candidates:
            changes = {"main_category": candidates[0]["name"], "subcategory": "", "classification_status": "needs_review"}
            if not dry:
                ws.edit(bid, changes)
                book.update(ws.book(bid))
            else:
                m.update(changes)
            summary["classified_auto"] += 1
            summary["needs_classification"].append(bid)
        else:
            summary["needs_classification"].append(bid)

    progress("[2/5] 核对版权与来源…")
    for book in active:
        control.check()
        bid, m = book["book_id"], book["metadata"]
        confirmed = (m.get("rights_review_status") == "confirmed"
                     and m.get("copyright_status") in {"authorized", "public_domain", "open_license"}
                     and str(m.get("source_reference") or "").strip())
        if confirmed:
            continue
        if opts.get("auto_rights"):
            changes = {"copyright_status": opts["rights_status"],
                       "source_reference": opts["source_reference"].strip(), "rights_review_status": "confirmed"}
            if not dry:
                ws.edit(bid, changes)
                book.update(ws.book(bid))
            else:
                m.update(changes)
            summary["rights_auto"] += 1
        else:
            summary["needs_rights"].append(bid)
    checkpoint()
    uploadable = [book for book in active if _qualified(ws, book)]

    progress("[3/5] 上传并验证封面…")
    cover_todo = []
    for book in uploadable:
        if not book.get("cover_path"):
            summary["cover_skipped_none"] += 1
        elif not force and cover_is_current(book, ws.result(book["book_id"], "r2"), config):
            summary["cover_verified_skip"] += 1
        else:
            cover_todo.append(book)
    if dry:
        summary["_cover_would"] = len(cover_todo)
    else:
        for book in cover_todo:
            control.check()
            bid = book["book_id"]
            try:
                upload_cover(ws, bid, config, credentials)
                summary["cover_uploaded"] += 1
                progress("封面已验证：" + bid)
            except Cancelled:
                raise
            except Exception as exc:
                summary["cover_failed"] += 1
                summary["errors"].append(f"cover {bid}: {exc}")
                progress("封面失败：" + bid + " / " + str(exc))
            checkpoint()

    progress(f"[4/5] 上传网盘（{provider}）…")
    todo = []
    for book in uploadable:
        if ws.result(book["book_id"], provider).get("share_url"):
            summary["book_skipped_done"] += 1
        else:
            todo.append(book)
    if dry:
        summary["_book_would"] = len(todo)
    elif todo:
        connector, gate_error = None, None
        if provider == "quark":
            try:
                # 保持现有夸克连接器与环境判断原样，仅在确需上传时调用。
                connector = quark_connector(ws, config)
            except Exception as exc:
                gate_error = error_message(exc)
        for book in todo:
            control.check()
            bid = book["book_id"]
            try:
                if gate_error:
                    raise ValueError(gate_error)
                # 百度由 upload_book 根据类别创建自己的连接器，不依赖夸克闸门。
                outcome = upload_book(ws, bid, provider, config, credentials, control, connector=connector)
                if not outcome.get("share_url"):
                    raise ValueError("未生成分享链接")
                summary["book_uploaded"] += 1
                progress("分享已生成：" + bid)
            except Cancelled:
                raise
            except Exception as exc:
                summary["book_failed"] += 1
                summary["errors"].append(f"book {bid}: {error_message(exc)}")
                progress("网盘失败：" + bid + " / " + error_message(exc))
            checkpoint()

    progress("[5/5] 核对并同步网站…")
    site_id, site_url = str(config.get("site_id") or ""), str(config.get("site_url") or "")
    site_todo = []
    for book in uploadable:
        control.check()
        bid = book["book_id"]
        if not ws.result(bid, provider).get("share_url"):
            continue
        current = book if dry else ws.book(bid)
        record = public_record(ws, current)
        saved = ws.result(bid, "site:" + site_id)
        fingerprint = sync_fingerprint(record, site_url, site_id, publish, overwrite)
        if (not force and saved.get("status") == "ok" and saved.get("_sync_fingerprint") == fingerprint
                and (not publish or saved.get("publish_status") == "published")):
            summary["site_skipped"] += 1
        else:
            site_todo.append(bid)
    if dry:
        summary["_site_would"] = len(site_todo)
        return
    if not site_todo:
        return
    client = None
    try:
        client = SiteClient(ws, config, credentials)
        info = client.info()
        if info.get("site_id") != site_id:
            raise ValueError("网站编号不一致")
    except Exception as exc:
        if client:
            client.close()
        summary["site_failed"] += len(site_todo)
        summary["errors"].append("site: " + str(exc))
        checkpoint()
        return
    try:
        for offset in range(0, len(site_todo), batch_size):
            control.check()
            chunk = site_todo[offset:offset + batch_size]
            try:
                preview = client.preview(chunk)
                rows = {r["book_id"]: r for r in preview["rows"]}
            except Cancelled:
                raise
            except Exception as exc:
                summary["site_failed"] += len(chunk)
                summary["errors"].append("preview: " + str(exc))
                checkpoint()
                continue
            choices = []
            for bid in chunk:
                row = rows.get(bid)
                if not row or row.get("error"):
                    summary["site_failed"] += 1
                    summary["errors"].append(f"preview {bid}: {row.get('error') if row else '缺少预检行'}")
                elif row.get("action") not in {"create", "update"}:
                    summary["needs_binding"].append(bid)
                    summary["site_skipped"] += 1
                    progress("需要人工选择网站对应资源，未自动绑定：" + bid)
                else:
                    choices.append({"book_id": bid, "action": row["action"], "publish": publish, "overwrite": overwrite})
            if not choices:
                checkpoint()
                continue
            try:
                response = client.commit(preview, choices, control, progress)
                items = (response or {}).get("items", {})
                for choice in choices:
                    bid = choice["book_id"]
                    item = items.get(bid, {})
                    if item.get("status") != "ok":
                        summary["site_failed"] += 1
                        summary["errors"].append(f"site {bid}: {item.get('message') or '缺少成功回执'}")
                    else:
                        summary["site_synced"] += 1
                        if item.get("publish_status") == "published":
                            summary["site_published"] += 1
                        elif publish:
                            summary["site_unpublished"] += 1
                            progress("资料已同步，但尚未发布（请检查链接或归档状态）：" + bid)
            except Cancelled:
                raise
            except Exception as exc:
                summary["site_failed"] += len(choices)
                summary["errors"].append("commit: " + str(exc))
            checkpoint()
    finally:
        client.close()


def format_summary(summary):
    lines = [
        f"范围：{summary['total']} 本｜排除/异常跳过：{summary.get('excluded_skipped', 0)} 本",
        f"分类确认：{summary['classified_auto']}｜待人工分类：{len(summary['needs_classification'])}",
        f"版权确认：{summary['rights_auto']}｜待人工确认：{len(summary['needs_rights'])}",
        f"封面：上传 {summary['cover_uploaded']}｜无封面 {summary['cover_skipped_none']}｜已验证 {summary['cover_verified_skip']}｜失败 {summary['cover_failed']}",
        f"网盘：上传并分享 {summary['book_uploaded']}｜已有分享 {summary['book_skipped_done']}｜失败 {summary['book_failed']}",
        f"网站：同步 {summary['site_synced']}｜跳过 {summary['site_skipped']}｜失败 {summary['site_failed']}｜待人工绑定 {len(summary.get('needs_binding', []))}",
        f"发布：已发布 {summary.get('site_published', 0)}｜已请求但未发布 {summary.get('site_unpublished', 0)}",
    ]
    if "_cover_would" in summary:
        lines.append(f"预演预计：封面 {summary['_cover_would']}｜网盘 {summary.get('_book_would', 0)}｜已有分享可同步 {summary.get('_site_would', 0)}；未写入、未联网")
    if summary["errors"]:
        lines.append(f"错误 {len(summary['errors'])} 条（首条：{summary['errors'][0]}）")
    return "\n".join(lines)
