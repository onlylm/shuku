from __future__ import annotations

import argparse
import getpass

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import AdminUser


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="创建或重置网站管理员")
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--display-name")
    args = parser.parse_args()
    password = args.password or getpass.getpass("管理员密码：")
    if not 12 <= len(password) <= 128:
        raise SystemExit("密码需为12—128个字符")
    with SessionLocal() as db:
        username = args.username
        if not username:
            users = list(db.scalars(select(AdminUser).order_by(AdminUser.id).limit(2)))
            if len(users) > 1:
                raise SystemExit("存在多个管理员，请通过 --username 明确指定要重置的账号")
            username = users[0].username if users else settings.default_admin_username
        user = db.scalar(select(AdminUser).where(AdminUser.username == username))
        if user:
            user.password_hash = hash_password(password)
            if args.display_name:
                user.display_name = args.display_name
            user.is_active = True
            action = "已重置"
        else:
            user = AdminUser(
                username=username,
                password_hash=hash_password(password),
                display_name=args.display_name or "管理员",
            )
            db.add(user)
            action = "已创建"
        db.commit()
    print(f"{action}管理员：{username}")


if __name__ == "__main__":
    main()
