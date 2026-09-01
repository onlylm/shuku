from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import AdminOperationLog, Category, ChannelShareLink, OrganizerBatch, OrganizerIdentity, Resource, ResourceFile
from app.models.base import utcnow
from app.providers import registry, url_hash
from app.services.links import add_or_replace_link, check_link
from app.services.organizer_contract import CommitChoices, OrganizerPackage
from app.services.resources import create_resource
from app.services.text import normalize_title
from app.services.category_governance import resolve_categories
from app.services.catalog_layout import materialize, plan_data, same_plan
from app.services.publication import apply_publication_gate, publication_issues
from app.services.site_settings import cover_hosts


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def fingerprint(resource):
    # 访客浏览量不是元数据修改，不应使预检无端失效。
    def stable(value):
        if isinstance(value, datetime):
            return (value.astimezone(timezone.utc) if value.tzinfo else value).replace(tzinfo=None).isoformat()
        return value
    return digest({c.name: stable(getattr(resource, c.name)) for c in Resource.__table__.columns if c.name not in {"view_count", "updated_at", "created_at"}} | {"categories": sorted(c.id for c in resource.categories)})


def validate_package(package, db):
    settings = get_settings()
    if package.site_id != settings.organizer_site_id:
        raise ValueError("站点编号不匹配，不能将本地编号绑定到其他站点")
    if len({b.book_id for b in package.books}) != len(package.books):
        raise ValueError("同批次图书编号重复")
    allowed = set(cover_hosts(db, settings))
    for book in package.books:
        if book.cover_url:
            url = urlsplit(book.cover_url)
            if url.scheme != "https" or not url.hostname or url.hostname.lower() not in allowed or url.username or url.password or url.port not in {None, 443}:
                raise ValueError("封面域名尚未获准或不是安全 HTTPS 地址")
        for link in book.links:
            registry.recognize(link.url, link.extract_code)


def make_preview(db: Session, package: OrganizerPackage, token_id: int):
    validate_package(package, db)
    data = package.model_dump()
    payload_hash = digest(data)
    existing = db.get(OrganizerBatch, package.export_id)
    if existing:
        if existing.token_id != token_id or existing.payload_hash != payload_hash:
            raise ValueError("批次编号已存在，但内容或授权来源不一致")
        return existing
    rows = []
    for book in package.books:
        identity = db.get(OrganizerIdentity, book.book_id)
        candidates = []
        action = "create"
        error = None
        if identity:
            resource = db.get(Resource, identity.resource_id)
            candidates = [resource] if resource else []
            action = "update"
            if book.epub_sha256 != identity.epub_sha256:
                error = "相同图书编号的文件内容改变，需要人工版本处理"
            elif book.revision < identity.revision:
                error = "旧修订不能覆盖新修订"
            elif book.revision == identity.revision and digest(book.model_dump()) != identity.payload_hash:
                error = "同一修订的内容不一致，请在本地保存修改后重新导出"
        else:
            conditions = [Resource.normalized_title == normalize_title(book.title)]
            if book.isbn:
                conditions.append(Resource.isbn == book.isbn)
            candidates = list(db.scalars(select(Resource).where(or_(*conditions)).limit(30)))
            if candidates:
                action = "choose"
        warnings = []
        if book.rights_review_status != "confirmed" or not book.copyright_status or not (book.source_reference or "").strip():
            warnings.append("版权类别及来源未确认，只能保存草稿")
        try:
            path = resolve_categories(db, book.main_category, book.subcategory, allow_planned=True)
            mapped_categories = plan_data(path)
            if path and path[-1].id is None:
                warnings.append(f"提交后将在“{path[0].name}”下创建二级分类“{path[-1].name}”，顶部导航不变")
        except ValueError as exc:
            warnings.append(str(exc))
            mapped_categories = []
            path = []
        warnings.extend(publication_issues(SimpleNamespace(title=book.title, author=book.author, publisher=book.publisher,
            copyright_status=book.copyright_status if book.rights_review_status == "confirmed" else "pending",
            source_reference=book.source_reference, categories=path)))
        if any(r.metadata_locked for r in candidates):
            warnings.append("网站人工审核资料已保护，同步不会改写书目字段和分类；分享链接仍可补充")
        rows.append({"book_id": book.book_id, "title": book.title, "action": action, "error": error,
                     "warnings": list(dict.fromkeys(warnings)), "mapped_categories": mapped_categories,
                     "candidates": [{"id": r.id, "title": r.title, "author": r.author, "resource_code": r.resource_code, "fingerprint": fingerprint(r), "current": {key: getattr(r, key) for key in ("title", "author", "publisher", "isbn", "description", "cover_image", "copyright_status", "source_reference", "publish_status", "metadata_locked")}} for r in candidates],
                     "incoming": book.model_dump()})
    batch = OrganizerBatch(id=package.export_id, token_id=token_id, payload_hash=payload_hash, payload=data, preview=rows, receipt={})
    db.add(batch)
    db.commit()
    return batch


def commit_batch(db: Session, batch: OrganizerBatch, choices: CommitChoices, admin_id: int):
    # 预检与提交之间管理员可能调整封面白名单，提交时必须再次校验。
    validate_package(OrganizerPackage.model_validate(batch.payload), db)
    if len({c.book_id for c in choices.choices}) != len(choices.choices):
        raise ValueError("提交选择重复")
    rows = {r["book_id"]: r for r in batch.preview}
    receipt = dict(batch.receipt or {})
    for choice in choices.choices:
        # 先取写锁，再重读回执；并发重放不能覆盖已经成功的记录。
        db.execute(update(OrganizerBatch).where(OrganizerBatch.id == batch.id).values(updated_at=utcnow()))
        db.refresh(batch)
        receipt = dict(batch.receipt or {})
        if choice.book_id in receipt and receipt[choice.book_id].get("status") == "ok":
            db.commit()
            continue
        try:
            with db.begin_nested():
                row = rows.get(choice.book_id)
                if not row or row["error"]:
                    raise ValueError(row["error"] if row else "图书不在此预检中")
                if "mapped_categories" not in row:
                    raise ValueError("预检版本过旧，请使用当前网站重新预检")
                book = row["incoming"]
                identity = db.scalar(select(OrganizerIdentity).where(OrganizerIdentity.book_id == choice.book_id).with_for_update().execution_options(populate_existing=True))
                if identity and identity.epub_sha256 != book["epub_sha256"]:
                    raise ValueError("内容哈希不一致")
                if identity and identity.revision > book["revision"]:
                    raise ValueError("旧修订不能覆盖新修订")
                if identity and identity.revision == book["revision"] and digest(book) != identity.payload_hash:
                    raise ValueError("同一修订内容不一致，请重新导出")
                if choice.action == "update" and not identity:
                    raise ValueError("系统编号尚未绑定，请明确选择绑定对象")
                if choice.action == "create":
                    if identity:
                        raise ValueError("该编号已有绑定，请重新预检")
                    resource = create_resource(db, {**book, "publish_status": "draft", "cover_image": book["cover_url"],
                                                   "copyright_status": book["copyright_status"] if book["rights_review_status"] == "confirmed" else "pending"})
                else:
                    rid = identity.resource_id if identity else choice.resource_id
                    candidate = next((c for c in row["candidates"] if c["id"] == rid), None)
                    resource = db.scalar(select(Resource).where(Resource.id == rid).with_for_update().execution_options(populate_existing=True)) if rid else None
                    if not resource or not candidate or fingerprint(resource) != candidate["fingerprint"]:
                        raise ValueError("预检后网站资料已改变或未明确选择，须重新预检")
                    other = db.scalar(select(OrganizerIdentity).where(OrganizerIdentity.resource_id == resource.id))
                    if other and other.book_id != choice.book_id:
                        raise ValueError("该网站图书已经对应其他版本编号")
                warnings = []
                classification_error = None
                if resource.metadata_locked:
                    warnings.append("已保留网站人工审核资料和分类")
                else:
                    for field in ("subtitle", "author", "translator", "publisher", "isbn", "description", "language", "publish_year", "source_reference", "copyright_status"):
                        if field in {"copyright_status", "source_reference"} and book["rights_review_status"] != "confirmed":
                            continue
                        empty = not getattr(resource, field) or (field == "copyright_status" and resource.copyright_status == "pending")
                        if book.get(field) and (choice.overwrite or empty):
                            setattr(resource, field, book[field])
                    if choice.overwrite:
                        resource.title = book["title"]
                        resource.normalized_title = normalize_title(book["title"])
                        resource.subtitle = book.get("subtitle") or None
                    if book["cover_url"] and (choice.overwrite or not resource.cover_image):
                        resource.cover_image = book["cover_url"]
                    try:
                        current_path = resolve_categories(db, book.get("main_category"), book.get("subcategory"), allow_planned=True)
                    except ValueError as exc:
                        current_path = []
                        classification_error = str(exc)
                        warnings.append(classification_error)
                    if not same_plan(current_path, row["mapped_categories"]):
                        raise ValueError("预检后分类映射已改变，请重新预检")
                    if current_path:
                        resource.categories = materialize(db, current_path)
                resource.source_category_main = book.get("main_category") or None
                resource.source_category_sub = book.get("subcategory") or None
                resource.formats = " · ".join(sorted(set((resource.formats or "").replace("·", " ").split()) | {"EPUB"}))
                blockers = publication_issues(resource)
                if classification_error:
                    blockers.append(classification_error)
                if not resource.metadata_locked and book["rights_review_status"] != "confirmed":
                    blockers.append("本地版权审核尚未确认")
                linked = []
                verified_now = set()
                for incoming in book["links"]:
                    parsed = registry.recognize(incoming["url"], incoming.get("extract_code"))
                    link = db.scalar(select(ChannelShareLink).where(ChannelShareLink.normalized_url_hash == url_hash(parsed.normalized_url)))
                    if link and link.channel.resource_id != resource.id:
                        raise ValueError("分享链接已属于另一条资源，未覆盖")
                    if not link:
                        link = add_or_replace_link(db, resource.id, incoming["url"], incoming.get("extract_code"))
                    if choice.publish:
                        log = check_link(db, link)
                        if log.result == "ok":
                            verified_now.add(link.id)
                    linked.append(link)
                if choice.publish and not blockers and any(l.id in verified_now and l.status == "active" and l.is_visible and l.provider.status == "active" and l.channel.status == "active" for l in linked):
                    if resource.publish_status != "archived":
                        resource.publish_status = "published"
                        resource.published_at = resource.published_at or utcnow()
                if resource.publish_status == "published" and blockers and not resource.metadata_locked:
                    resource.publish_status = "draft"
                apply_publication_gate(resource)
                if choice.publish and resource.publish_status != "published":
                    warnings.extend(blockers or ["未获得本次检测有效的网盘链接，未自动发布"])
                if not identity:
                    identity = OrganizerIdentity(book_id=choice.book_id, resource_id=resource.id, epub_sha256=book["epub_sha256"], revision=book["revision"], payload_hash=digest(book))
                    db.add(identity)
                identity.revision, identity.payload_hash = book["revision"], digest(book)
                if not db.scalar(select(ResourceFile.id).where(ResourceFile.resource_id == resource.id, ResourceFile.checksum_sha256 == book["epub_sha256"])):
                    db.add(ResourceFile(resource_id=resource.id, file_name=choice.book_id + ".epub", file_format="EPUB", checksum_sha256=book["epub_sha256"], source_type="organizer"))
                db.add(AdminOperationLog(admin_user_id=admin_id, action="organizer_commit", entity_type="resource", entity_id=str(resource.id), detail={"book_id": choice.book_id, "export_id": batch.id, "publish_requested": choice.publish}))
                db.flush()
                result = {"status": "ok", "resource_id": resource.id, "resource_code": resource.resource_code, "publish_status": resource.publish_status, "revision": identity.revision, "warnings": list(dict.fromkeys(warnings)), "publication_issues": blockers, "links": [{"id": l.id, "status": l.status} for l in linked]}
            receipt[choice.book_id] = result
        except Exception as exc:
            receipt[choice.book_id] = {"status": "error", "message": str(exc)[:250] if isinstance(exc, ValueError) else "数据库冲突或服务异常，请重新预检后重试"}
        batch.receipt = dict(receipt)
        db.commit()
    return {"site_id": batch.payload["site_id"], "export_id": batch.id, "items": receipt}
