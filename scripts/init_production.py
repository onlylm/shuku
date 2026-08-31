"""幂等初始化正式站点，不重置已有管理员，不创建演示资源。"""
from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import AdminUser, Category, Provider
from app.catalog_v1 import GROUPS


DEFAULT_CATEGORIES = [(name, "catalog-" + code) for code, name, _, _ in GROUPS]


def initialize(db: Session, username: str, password: str) -> bool:
    created = False
    if db.scalar(select(AdminUser.id).limit(1)) is None:
        if len(password) < 16 or password in {"ChangeMe123!", "change-this-before-first-run"}:
            raise ValueError("首次部署必须提供至少16位随机管理员密码")
        db.add(AdminUser(username=username, password_hash=hash_password(password), display_name="管理员"))
        created = True
    # 已有分类时不重新插入被运营者删除/修改的默认分类。
    if db.scalar(select(Category.id).limit(1)) is None:
        for index, (name, slug) in enumerate(DEFAULT_CATEGORIES, 1):
            db.add(Category(name=name, slug=slug, sort_order=index * 10))
    for code, name, domain, order in [
        ("baidu", "百度网盘", "pan.baidu.com", 10),
        ("quark", "夸克网盘", "pan.quark.cn", 20),
    ]:
        if db.scalar(select(Provider.id).where(Provider.code == code)) is None:
            db.add(Provider(code=code, name=name, base_domain=domain, sort_order=order,
                            capabilities={"recognize": True, "health_check": True}))
    db.commit()
    return created


def main() -> None:
    from app.core.config import get_settings
    from app.core.database import SessionLocal

    settings = get_settings()
    with SessionLocal() as db:
        created = initialize(db, settings.default_admin_username, os.environ.get("INITIAL_ADMIN_PASSWORD", ""))
    print("管理员已创建，请使用首次生成的密码登录。" if created else "保留已有管理员及密码，不做重置。")
    print("基础网盘渠道已就绪；未创建任何演示图书。")


if __name__ == "__main__":
    main()
