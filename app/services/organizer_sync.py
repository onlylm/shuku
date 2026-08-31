from __future__ import annotations

import hashlib
import json
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
from app.services.text import normalize_title, slugify


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def fingerprint(resource):
    return digest({c.name: getattr(resource, c.name) for c in Resource.__table__.columns} | {"categories": sorted(c.id for c in resource.categories)})


def validate_package(package):
    settings = get_settings()
    if package.site_id != settings.organizer_site_id:
        raise ValueError("站点编号不匹配，不能将本地编号绑定到其他站点")
    if len({b.book_id for b in package.books}) != len(package.books):
        raise ValueError("同批次图书编号重复")
    allowed = {host.strip().lower() for host in settings.organizer_cover_hosts.split(",") if host.strip()}
    for book in package.books:
        if book.cover_url:
            url = urlsplit(book.cover_url)
            if url.scheme != "https" or not url.hostname or url.hostname.lower() not in allowed or url.username or url.password or url.port not in {None, 443}:
                raise ValueError("封面域名尚未获准或不是安全 HTTPS 地址")
        for link in book.links:
            registry.recognize(link.url, link.extract_code)


def make_preview(db: Session, package: OrganizerPackage, token_id: int):
    validate_package(package)
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
        else:
            conditions = [Resource.normalized_title == normalize_title(book.title)]
            if book.isbn:
                conditions.append(Resource.isbn == book.isbn)
            candidates = list(db.scalars(select(Resource).where(or_(*conditions)).limit(30)))
            if candidates:
                action = "choose"
        if book.rights_review_status != "confirmed" or not book.copyright_status or not (book.source_reference or "").strip():
            error = "请先确认版权类别及来源说明"
        rows.append({"book_id": book.book_id, "title": book.title, "action": action, "error": error,
                     "candidates": [{"id": r.id, "title": r.title, "author": r.author, "resource_code": r.resource_code, "fingerprint": fingerprint(r), "current": {key: getattr(r, key) for key in ("title", "author", "publisher", "isbn", "description", "cover_image", "copyright_status", "source_reference", "publish_status")}} for r in candidates],
                     "incoming": book.model_dump()})
    batch = OrganizerBatch(id=package.export_id, token_id=token_id, payload_hash=payload_hash, payload=data, preview=rows, receipt={})
    db.add(batch)
    db.commit()
    return batch


def category_for(db, name, parent=None):
    name = (name or "").strip()
    if not name:
        return None
    rows = list(db.scalars(select(Category).where(Category.name == name, Category.parent_id == (parent.id if parent else None))))
    if len(rows) > 1:
        raise ValueError("存在同父类同名分类，需要后台整理")
    if rows:
        return rows[0]
    slug = slugify(name)[:85] + "-" + digest([parent.id if parent else None, name])[:12]
    category = Category(name=name, slug=slug, parent_id=parent.id if parent else None)
    db.add(category)
    db.flush()
    return category


def commit_batch(db: Session, batch: OrganizerBatch, choices: CommitChoices, admin_id: int):
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
                book = row["incoming"]
                identity = db.get(OrganizerIdentity, choice.book_id)
                if identity and identity.epub_sha256 != book["epub_sha256"]:
                    raise ValueError("内容哈希不一致")
                if identity and identity.revision > book["revision"]:
                    raise ValueError("旧修订不能覆盖新修订")
                if choice.action == "create":
                    if identity:
                        raise ValueError("该编号已有绑定，请重新预检")
                    resource = create_resource(db, {**book, "publish_status": "draft", "cover_image": book["cover_url"]})
                else:
                    rid = identity.resource_id if identity else choice.resource_id
                    candidate = next((c for c in row["candidates"] if c["id"] == rid), None)
                    resource = db.get(Resource, rid) if rid else None
                    if not resource or not candidate or fingerprint(resource) != candidate["fingerprint"]:
                        raise ValueError("预检后网站资料已改变或未明确选择，须重新预检")
                    other = db.scalar(select(OrganizerIdentity).where(OrganizerIdentity.resource_id == resource.id))
                    if other and other.book_id != choice.book_id:
                        raise ValueError("该网站图书已经对应其他版本编号")
                for field in ("subtitle", "author", "translator", "publisher", "isbn", "description", "language", "publish_year", "source_reference", "copyright_status"):
                    if book.get(field) and (choice.overwrite or not getattr(resource, field)):
                        setattr(resource, field, book[field])
                if choice.overwrite:
                    resource.title = book["title"]
                    resource.normalized_title = normalize_title(book["title"])
                if book["cover_url"] and (choice.overwrite or not resource.cover_image):
                    resource.cover_image = book["cover_url"]
                resource.formats = " · ".join(sorted(set((resource.formats or "").replace("·", " ").split()) | {"EPUB"}))
                parent = category_for(db, book.get("main_category"))
                child = category_for(db, book.get("subcategory"), parent) if parent else None
                for category in (parent, child):
                    if category and category not in resource.categories:
                        resource.categories.append(category)
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
                if choice.publish and any(l.id in verified_now and l.status == "active" and l.is_visible and l.provider.status == "active" and l.channel.status == "active" for l in linked):
                    if resource.publish_status != "archived":
                        resource.publish_status = "published"
                        resource.published_at = resource.published_at or utcnow()
                if not identity:
                    identity = OrganizerIdentity(book_id=choice.book_id, resource_id=resource.id, epub_sha256=book["epub_sha256"], revision=book["revision"], payload_hash=digest(book))
                    db.add(identity)
                identity.revision, identity.payload_hash = book["revision"], digest(book)
                if not db.scalar(select(ResourceFile.id).where(ResourceFile.resource_id == resource.id, ResourceFile.checksum_sha256 == book["epub_sha256"])):
                    db.add(ResourceFile(resource_id=resource.id, file_name=choice.book_id + ".epub", file_format="EPUB", checksum_sha256=book["epub_sha256"], source_type="organizer"))
                db.add(AdminOperationLog(admin_user_id=admin_id, action="organizer_commit", entity_type="resource", entity_id=str(resource.id), detail={"book_id": choice.book_id, "export_id": batch.id, "publish_requested": choice.publish}))
                db.flush()
                result = {"status": "ok", "resource_id": resource.id, "resource_code": resource.resource_code, "publish_status": resource.publish_status, "revision": identity.revision, "links": [{"id": l.id, "status": l.status} for l in linked]}
            receipt[choice.book_id] = result
        except Exception as exc:
            receipt[choice.book_id] = {"status": "error", "message": str(exc)[:250] if isinstance(exc, ValueError) else "数据库冲突或服务异常，请重新预检后重试"}
        batch.receipt = dict(receipt)
        db.commit()
    return {"site_id": batch.payload["site_id"], "export_id": batch.id, "items": receipt}
