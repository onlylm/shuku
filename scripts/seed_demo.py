from __future__ import annotations

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import AdminUser, Category, ChannelShareLink, Provider, Resource, ResourceChannel
from app.models.base import utcnow
from app.providers import registry, url_hash
from app.services.resources import create_resource


CATEGORIES = [
    ("文学小说", "literature", "经典小说、散文与诗歌。"),
    ("历史人文", "history-humanities", "历史、哲学与人文通识。"),
    ("社会科学", "social-science", "社会学、经济与公共知识。"),
    ("编程开发", "programming", "开放许可的编程教程与技术手册。"),
    ("艺术设计", "art-design", "艺术史、设计与创作方法。"),
    ("公开课程", "open-courses", "开放课程讲义和学习资料。"),
]

PROVIDERS = [
    ("baidu", "百度网盘", "pan.baidu.com", 10),
    ("quark", "夸克网盘", "pan.quark.cn", 20),
]

BOOKS = [
    {
        "title": "月亮与六便士",
        "author": "毛姆",
        "formats": "EPUB · PDF",
        "category": "文学小说",
        "description": "关于理想、选择与自由的一部经典小说。此条为本地界面演示数据。",
        "copyright_status": "public_domain",
        "url": "https://pan.baidu.com/s/1LocalDemoMoon?pwd=p123",
    },
    {
        "title": "Python编程入门",
        "author": "开放教程社区",
        "formats": "PDF · EPUB",
        "category": "编程开发",
        "description": "面向初学者的开放教程演示条目。",
        "copyright_status": "open_license",
        "url": "https://pan.quark.cn/s/LocalDemoPython",
    },
    {
        "title": "论语选读",
        "author": "中华经典整理组",
        "formats": "PDF · MOBI",
        "category": "历史人文",
        "description": "用于阶段一本地测试的公版经典条目。",
        "copyright_status": "public_domain",
        "url": "https://pan.baidu.com/s/1LocalDemoLunyu",
    },
]


def main() -> None:
    settings = get_settings()
    if settings.app_env == "production":
        raise SystemExit("正式环境禁止导入演示数据，请使用 scripts.init_production 初始化。")
    with SessionLocal() as db:
        admin = db.scalar(select(AdminUser).where(AdminUser.username == settings.default_admin_username))
        if not admin:
            db.add(
                AdminUser(
                    username=settings.default_admin_username,
                    password_hash=hash_password(settings.default_admin_password),
                    display_name="本地管理员",
                )
            )
        for index, (name, slug, description) in enumerate(CATEGORIES, 1):
            if not db.scalar(select(Category.id).where(Category.slug == slug)):
                db.add(Category(name=name, slug=slug, description=description, sort_order=index * 10))
        for code, name, domain, order in PROVIDERS:
            if not db.scalar(select(Provider.id).where(Provider.code == code)):
                db.add(
                    Provider(
                        code=code,
                        name=name,
                        base_domain=domain,
                        sort_order=order,
                        capabilities={"recognize": True, "health_check": True},
                    )
                )
        db.commit()
        categories = {item.name: item for item in db.scalars(select(Category))}
        providers = {item.code: item for item in db.scalars(select(Provider))}
        for data in BOOKS:
            resource = db.scalar(select(Resource).where(Resource.normalized_title == data["title"].casefold()))
            if not resource:
                resource = create_resource(
                    db,
                    {
                        "title": data["title"],
                        "author": data["author"],
                        "formats": data["formats"],
                        "description": data["description"],
                        "copyright_status": data["copyright_status"],
                        "source_reference": "本地演示数据，不用于正式上线",
                        "publish_status": "published",
                    },
                )
                resource.categories.append(categories[data["category"]])
            parsed = registry.recognize(data["url"])
            digest = url_hash(parsed.normalized_url)
            if not db.scalar(select(ChannelShareLink.id).where(ChannelShareLink.normalized_url_hash == digest)):
                provider = providers[parsed.provider_code]
                channel = db.scalar(
                    select(ResourceChannel).where(
                        ResourceChannel.resource_id == resource.id,
                        ResourceChannel.provider_id == provider.id,
                    )
                )
                if not channel:
                    channel = ResourceChannel(resource=resource, provider=provider, status="active")
                    db.add(channel)
                    db.flush()
                db.add(
                    ChannelShareLink(
                        channel=channel,
                        provider_id=provider.id,
                        provider_share_id=parsed.share_id,
                        share_url=parsed.normalized_url,
                        normalized_url=parsed.normalized_url,
                        normalized_url_hash=digest,
                        extract_code=parsed.extract_code,
                        status="active",
                        is_visible=True,
                        last_checked_at=utcnow(),
                        last_ok_at=utcnow(),
                    )
                )
        db.commit()
    print("演示分类、渠道、资源和本地管理员已准备完成。")
    print(f"本地登录：{settings.default_admin_username} / {settings.default_admin_password}")
    print("上线前必须修改默认密码并移除或替换演示资源。")


if __name__ == "__main__":
    main()
