from __future__ import annotations

import secrets
import hashlib
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdminUser


password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


def authenticate_admin(db: Session, username: str, password: str) -> AdminUser | None:
    user = db.scalar(select(AdminUser).where(AdminUser.username == username.strip()))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return None
    return user


def current_admin(request: Request, db: Session) -> AdminUser | None:
    user_id = request.session.get("admin_user_id")
    if not user_id:
        return None
    try:
        user = db.get(AdminUser, int(user_id))
    except (ValueError, TypeError):
        return None
    expected = session_fingerprint(user) if user else ""
    supplied = str(request.session.get("admin_auth", ""))
    return user if user and user.is_active and secrets.compare_digest(expected, supplied) else None


def session_fingerprint(user: AdminUser) -> str:
    # 改用户名或密码（包括命令行重置）后，所有旧会话立即失效。
    return hashlib.sha256((user.username + ":" + user.password_hash).encode()).hexdigest()


def require_admin(request: Request, db: Session) -> AdminUser:
    user = current_admin(request, db)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    return user


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get("csrf_token")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="表单已过期，请刷新后重试")


class LoginRateLimiter:
    """阶段一单进程限流；部署多实例时再替换为共享存储。"""

    def __init__(self, attempts: int = 5, window_seconds: int = 600) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        entries = self._entries[key]
        while entries and entries[0] < now - self.window_seconds:
            entries.popleft()
        return len(entries) < self.attempts

    def record_failure(self, key: str) -> None:
        self._entries[key].append(time.monotonic())

    def clear(self, key: str) -> None:
        self._entries.pop(key, None)


login_rate_limiter = LoginRateLimiter()
