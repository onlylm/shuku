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
    parser = argparse.ArgumentParser(description="创建或重置静页书房管理员")
    parser.add_argument("--username", default=settings.default_admin_username)
    parser.add_argument("--password")
    parser.add_argument("--display-name", default="管理员")
    args = parser.parse_args()
    password = args.password or getpass.getpass("管理员密码：")
    if len(password) < 10:
        raise SystemExit("密码至少需要 10 个字符")
    with SessionLocal() as db:
        user = db.scalar(select(AdminUser).where(AdminUser.username == args.username))
        if user:
            user.password_hash = hash_password(password)
            user.display_name = args.display_name
            user.is_active = True
            action = "已重置"
        else:
            user = AdminUser(
                username=args.username,
                password_hash=hash_password(password),
                display_name=args.display_name,
            )
            db.add(user)
            action = "已创建"
        db.commit()
    print(f"{action}管理员：{args.username}")


if __name__ == "__main__":
    main()
