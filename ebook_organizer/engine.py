from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import uuid
from pathlib import Path

from .covers import make_cover
from .epub import inspect_epub
from .safeio import Cancelled, Control, atomic_bytes, atomic_json, is_link, safe_name, sha256_file, filesystem_path
from .workspace import Workspace, now


DEFAULT_RULES = {
    "计算机互联网": ["编程", "Python", "计算机", "算法", "人工智能"],
    "教育学习": ["教育", "学习方法", "考试"], "经济金融": ["经济", "金融", "投资"],
    "军事": ["军事", "战争"], "科学科普": ["科学", "物理", "宇宙", "生物"],
    "历史文化": ["历史", "朝代", "古代"], "人物传记": ["传记", "自传", "回忆录"],
    "商业管理": ["管理", "商业", "营销"], "少儿读物": ["儿童", "绘本", "童话"],
    "社会科学": ["社会学", "政治", "法律"], "生活实用": ["生活", "烹饪", "园艺"],
    "文学小说": ["小说", "散文", "诗歌", "文学"], "心理学": ["心理", "认知"],
    "艺术设计": ["艺术", "设计", "绘画", "摄影"], "哲学思想": ["哲学", "思想"],
}


def classify(path: Path, root: Path, metadata: dict, rules: dict) -> dict:
    parts = path.relative_to(root).parts[:-1]
    if root.name in rules:
        parts = (root.name,) + parts
    if parts and parts[0] in rules:
        return {"main_category": parts[0], "subcategory": parts[1] if len(parts) > 1 else "", "classification_status": "confirmed", "classification_evidence": "已知分类目录：" + "/".join(parts[:2])}
    text = (metadata.get("title", "") + " " + " ".join(metadata.get("subjects", []))).casefold()
    matches = [(category, [word for word in words if word.casefold() in text]) for category, words in rules.items()]
    matches = sorted([item for item in matches if item[1]], key=lambda item: len(item[1]), reverse=True)[:3]
    return {"main_category": "", "subcategory": "", "classification_status": "suggested" if matches else "pending", "classification_candidates": [{"name": name, "evidence": words} for name, words in matches], "classification_evidence": "关键词建议，须人工确认" if matches else "没有明确分类证据"}


def scan(workspace: Workspace, root: Path, control=None, progress=lambda _: None):
    control = control or Control()
    if is_link(root):
        raise ValueError("不扫描符号链接或目录联接")
    root = filesystem_path(root.resolve())
    if not root.is_dir() or root.is_relative_to(workspace.root) or workspace.root.is_relative_to(root) or is_link(root):
        raise ValueError("源目录必须存在，且不能与工作区互相包含或是目录联接")
    job_id = workspace.start_job("scan", {"root": str(root)})
    count, duplicates = 0, 0
    rules = workspace.setting("category_rules", DEFAULT_RULES)
    try:
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=lambda exc: workspace.event("scan", "目录无法访问：" + str(exc.filename))):
            control.check()
            dirs[:] = sorted(d for d in dirs if not is_link(Path(directory) / d))
            epub_names = [name for name in sorted(names) if Path(name).suffix.lower() == ".epub"]
            if not epub_names and any(Path(n).suffix.lower() == ".opf" or Path(n).stem.lower() == "cover" for n in names):
                workspace.event("scan", f"孤立附件目录：{directory}")
            for name in epub_names:
                control.check()
                path = Path(directory) / name
                if is_link(path) or not path.resolve().is_relative_to(root):
                    workspace.event("scan", "跳过符号链接：" + name)
                    continue
                progress(f"检测：{name}")
                try:
                    before = path.stat()
                    checksum = sha256_file(path, control)
                except OSError:
                    workspace.event("scan", "文件无法读取：" + str(path))
                    continue
                result = inspect_epub(path, control)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    workspace.event("scan", "扫描期间源文件改变，已跳过：" + name)
                    continue
                with workspace.connect() as db:
                    old = workspace.decode(db.execute("SELECT * FROM books WHERE sha256=?", (checksum,)).fetchone())
                book_id = old["book_id"] if old else "BK_" + uuid.uuid4().hex
                metadata = {"title": path.stem, "rights_review_status": "pending", "copyright_status": "", "source_reference": "", **result.metadata}
                metadata.update(classify(path, root, metadata, rules))
                cover_path, version = None, None
                if old:
                    duplicates += 1
                    if old["metadata"].get("classification_status") == "confirmed":
                        for key in ("main_category", "subcategory", "classification_status", "classification_evidence"):
                            if key in old["metadata"]:
                                metadata[key] = old["metadata"][key]
                    for key in old["locked"]:
                        if key in old["metadata"]:
                            metadata[key] = old["metadata"][key]
                    # 同内容的外部附件冲突不覆盖第一次关联或人工选择。
                    cover_path, version = old["cover_path"], old["cover_version"]
                    if old["metadata"].get("title") != metadata.get("title"):
                        result.warn("SOURCE_METADATA_CONFLICT", "相同内容的来源资料不同，保留已有资料")
                        metadata = old["metadata"]
                if result.cover and not cover_path:
                    try:
                        cover_path, version = make_cover(result.cover, workspace.root, book_id)
                    except Exception as exc:
                        result.warn("COVER_DECODE_ERROR", "封面不能安全处理：" + type(exc).__name__)
                if result.external_opf:
                    opf = workspace.root / "originals" / book_id / (hashlib.sha256(result.external_opf).hexdigest() + ".opf")
                    if not opf.exists():
                        atomic_bytes(opf, result.external_opf)
                stamp = now()
                with workspace.connect() as db:
                    if not old:
                        db.execute("INSERT INTO books(book_id,sha256,metadata,provenance,issues,status,cover_path,cover_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (book_id, checksum, json.dumps(metadata, ensure_ascii=False), json.dumps(result.provenance, ensure_ascii=False), json.dumps(result.issues, ensure_ascii=False), result.status, cover_path, version, stamp, stamp))
                    else:
                        changed = metadata != old["metadata"] or version != old["cover_version"]
                        db.execute("UPDATE books SET metadata=?,provenance=?,issues=?,status=?,cover_path=?,cover_version=?,revision=revision+?,updated_at=? WHERE book_id=?", (json.dumps(metadata, ensure_ascii=False), json.dumps(result.provenance, ensure_ascii=False), json.dumps(result.issues, ensure_ascii=False), result.status, cover_path, version, int(changed), stamp, book_id))
                    db.execute("INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?)", (str(path), str(root), book_id, after.st_size, after.st_mtime_ns, stamp))
                count += 1
                progress(f"已检测 {count} 个文件；同内容/已存在 {duplicates} 个")
        result = {"files": count, "existing": duplicates}
        workspace.finish_job(job_id, "succeeded", result)
        return result
    except Exception as exc:
        workspace.finish_job(job_id, "cancelled" if isinstance(exc, Cancelled) else "failed", {"files": count, "error": type(exc).__name__, "root": str(root)})
        raise


def public_record(workspace: Workspace, book: dict):
    fields = {k: book["metadata"].get(k) for k in ("title", "subtitle", "author", "translator", "publisher", "isbn", "description", "language", "publish_year", "main_category", "subcategory", "rights_review_status", "copyright_status", "source_reference")}
    mapping = workspace.setting("site_category_mapping", {})
    local_category = "/".join(filter(None, [fields.get("main_category"), fields.get("subcategory")]))
    if mapped := mapping.get(local_category):
        fields.update(main_category=mapped[0], subcategory=mapped[1] if len(mapped) > 1 else "")
    cover = workspace.result(book["book_id"], "r2")
    links = []
    for provider in ("quark", "baidu"):
        outcome = workspace.result(book["book_id"], provider)
        if outcome.get("share_url"):
            links.append({"url": outcome["share_url"], "extract_code": outcome.get("extract_code")})
    return {**fields, "book_id": book["book_id"], "revision": book["revision"], "epub_sha256": book["sha256"], "formats": "EPUB", "cover_url": cover.get("url") if cover.get("state") == "verified" and cover.get("version") == book["cover_version"] else None, "links": links}


def export_snapshot(workspace: Workspace, ids: list[str], destination: Path, control=None, progress=lambda _: None):
    control = control or Control()
    if is_link(destination):
        raise ValueError("不向目录联接导出")
    destination = filesystem_path(destination.resolve())
    with workspace.connect() as db:
        roots = [Path(row[0]) for row in db.execute("SELECT DISTINCT root FROM sources")]
    if any(destination.is_relative_to(root) or root.is_relative_to(destination) for root in roots):
        raise ValueError("导出目录不能与源书库互相包含")
    if destination.is_relative_to(workspace.root) or workspace.root.is_relative_to(destination):
        raise ValueError("导出目录不能与工作区互相包含")
    if is_link(destination):
        raise ValueError("不向目录联接导出")
    selected = [workspace.book(book_id) for book_id in dict.fromkeys(ids)]
    export_id = uuid.uuid4().hex
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / (".staging_" + export_id)
    staging.mkdir()
    records, issues, manifest = [], [], []
    job = workspace.start_job("export", {"export_id": export_id})
    try:
        for book in selected:
            control.check()
            if not book or book["excluded"]:
                continue
            record = public_record(workspace, book)
            if book["status"] in {"failed", "blocked"}:
                issues.append({"book_id": book["book_id"], "issues": book["issues"]})
                continue
            metadata = book["metadata"]
            folder = Path("网盘上传") / safe_name(metadata.get("main_category") or "待人工分类")
            if metadata.get("subcategory"):
                folder /= safe_name(metadata["subcategory"])
            name = safe_name(metadata["title"] + " - " + metadata.get("author", ""), 80)
            relative = folder / (name + "__" + book["book_id"] + ".epub")
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = workspace.source(book["book_id"])
            if sha256_file(source, control) != book["sha256"]:
                raise ValueError("源文件内容改变，请重新扫描")
            progress("复制：" + metadata["title"])
            with source.open("rb") as src, target.with_suffix(".epub.part").open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    control.check()
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if sha256_file(target.with_suffix(".epub.part"), control) != book["sha256"]:
                raise ValueError("复制校验不一致，未提交导出")
            target.with_suffix(".epub.part").replace(target)
            record.update(epub_path=relative.as_posix(), integrity_status=book["status"], issues=book["issues"])
            if book["cover_path"]:
                for filename in ("cover.webp", "thumb.webp"):
                    cover_source = (workspace.root / book["cover_path"]).with_name(filename)
                    cover_relative = Path("网站封面") / book["book_id"] / book["cover_version"] / filename
                    atomic_bytes(staging / cover_relative, cover_source.read_bytes())
                record["cover_path"] = (Path("网站封面") / book["book_id"] / book["cover_version"] / "cover.webp").as_posix()
            records.append(record)
        package = {"schema_version": "2.0", "export_id": export_id, "workspace_id": workspace.setting("workspace_id"), "books": records, "generated_at": now()}
        atomic_json(staging / "数据/books.json", package)
        site_id = workspace.setting("connections", {}).get("site_id")
        if site_id and records:
            from app.services.organizer_contract import OrganizerBook, OrganizerPackage
            valid_books, schema_errors = [], []
            for r in records:
                try:
                    valid_books.append(OrganizerBook.model_validate({k: v for k, v in r.items() if k in OrganizerBook.model_fields}))
                except ValueError:
                    schema_errors.append({"book_id": r["book_id"], "message": "字段长度或格式不满足网站要求，请在软件内修正后重新预检"})
            for offset in range(0, len(valid_books), 500):
                filename = "jingye-import.v2.json" if len(valid_books) <= 500 else f"jingye-import.v2.part-{offset // 500 + 1:03d}.json"
                site_package = OrganizerPackage(site_id=site_id, workspace_id=workspace.setting("workspace_id"), export_id=export_id if not offset else uuid.uuid4().hex, books=valid_books[offset:offset + 500])
                atomic_json(staging / "数据" / filename, site_package.model_dump())
            if schema_errors:
                atomic_json(staging / "异常区/site-fields.json", schema_errors)
        ready = [r for r in records if r["cover_url"] and r["rights_review_status"] == "confirmed" and r["main_category"]]
        atomic_json(staging / "数据/books.ready.json", {**package, "books": ready})
        atomic_json(staging / "数据/upload-manifest.v2.json", {"schema_version": "2.0", "export_id": export_id, "books": [{key: r[key] for key in ("book_id", "epub_path", "epub_sha256")} for r in records]})
        atomic_json(staging / "异常区/issues.json", issues + [{"book_id": r["book_id"], "issues": r["issues"]} for r in records if r["issues"]])
        columns = ["book_id", "title", "author", "isbn", "main_category", "subcategory", "cover_url", "epub_path", "epub_sha256"]
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\r\n")
        writer.writeheader()
        for record in records:
            row = {}
            for key in columns:
                value = str(record.get(key) or "")
                row[key] = "'" + value if value.startswith("'") or value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value
            writer.writerow(row)
        atomic_bytes(staging / "数据/books.csv", stream.getvalue().encode("utf-8-sig"))
        atomic_bytes(staging / "README.txt", "本快照只复制原文件，不改变原书库。网盘上传目录只包含通过基础检查的 EPUB。标准 books.json 不是旧网站表格导入格式；请使用本地软件的网站预检。配置站点后另生成 jingye-import.v2.json，超过500本自动分片。封面和图书仍需确认授权，导出成功不表示已上传或已发布。".encode("utf-8"))
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                manifest.append({"path": path.relative_to(staging).as_posix(), "sha256": sha256_file(path, control), "size": path.stat().st_size})
        atomic_json(staging / "数据/manifest.json", {"export_id": export_id, "files": manifest, "csv_escape": "leading-apostrophe-v1"})
        target = destination / ("export_" + now()[:19].replace(":", "-") + "_" + export_id[:8])
        staging.rename(target)
        workspace.finish_job(job, "succeeded", {"path": str(target), "count": len(records)})
        return target
    except Exception as exc:
        workspace.finish_job(job, "cancelled" if isinstance(exc, Cancelled) else "failed", {"staging": str(staging), "error": type(exc).__name__})
        raise
