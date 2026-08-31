"""仅在隔离 CI 空数据库验证带旧书目的目录迁移；不用于真实网站。"""
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import func, select


def main():
    if os.environ.get("CATALOG_MIGRATION_TEST") != "synthetic-only":
        raise SystemExit("仅允许隔离 CI 使用；禁止在真实网站运行。")
    from app.core.database import SessionLocal
    from app.models import Category, Provider, Resource, ResourceFile, SiteSetting
    from app.services.resources import create_resource
    from app.services.links import add_or_replace_link

    with SessionLocal() as db:
        if db.scalar(select(func.count(Resource.id))) or db.scalar(select(func.count(Category.id))):
            raise SystemExit("测试数据库不是空的，未写入任何内容。")
        science = Category(name="计算机互联网", slug="ci-old-computer")
        lit = Category(name="文学", slug="ci-old-literature")
        db.add_all([science, lit, Provider(code="quark", name="夸克网盘", base_domain="pan.quark.cn")]); db.flush()
        ai = Category(name="人工智能", slug="ci-old-ai", parent_id=science.id)
        db.add(ai); db.flush()
        book = create_resource(db, {"title": "合成迁移验证书", "author": "合成作者", "publisher": "合成出版社",
            "category_ids": [science.id, ai.id], "copyright_status": "authorized",
            "source_reference": "仅限 CI 合成资料", "publish_status": "published", "metadata_locked": True})
        db.add(ResourceFile(resource_id=book.id, file_name="synthetic.epub", file_format="EPUB", source_type="ci"))
        # 仅解析合成链接并保存，不请求任何网盘或实际下载。
        link = add_or_replace_link(db, book.id, "https://pan.quark.cn/s/synthetic-ci")
        db.commit(); db.refresh(book)
        rid, leaf_id, link_id = book.id, ai.id, link.id
        before = {c.name: getattr(book, c.name) for c in Resource.__table__.columns}
        share_url = link.share_url
    command.upgrade(Config("alembic.ini"), "head")
    command.upgrade(Config("alembic.ini"), "head")
    with SessionLocal() as db:
        from app.models import ChannelShareLink, CategoryRedirect
        book = db.get(Resource, rid)
        assert {c.name: getattr(book, c.name) for c in Resource.__table__.columns} == before
        roots = db.get(SiteSetting, "catalog_layout").value["roots"]
        assert len(roots) == 8
        assert {c.id for c in book.categories} == {roots["science"], leaf_id}
        assert db.get(ChannelShareLink, link_id).share_url == share_url
        assert db.scalar(select(ResourceFile.file_name).where(ResourceFile.resource_id == rid)) == "synthetic.epub"
        assert db.scalar(select(CategoryRedirect.target_id).where(CategoryRedirect.source_id == lit.id)) == roots["literature"]
    print("带旧书目迁移及重复升级通过：书籍字段、文件记录、分享链接保持不变。")


if __name__ == "__main__":
    main()
