from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.services.site_settings import read_value


DEFAULT_TIMEZONE = "Asia/Shanghai"
TIMEZONE_CHOICES = (
    ("Asia/Shanghai", "北京时间（UTC+8）"),
    ("Asia/Hong_Kong", "香港时间（UTC+8）"),
    ("Asia/Tokyo", "东京时间（UTC+9）"),
    ("Asia/Singapore", "新加坡时间（UTC+8）"),
    ("Europe/London", "伦敦时间"),
    ("America/New_York", "纽约时间"),
    ("UTC", "协调世界时（UTC）"),
)
TIMEZONE_OFFSETS = {
    "Asia/Shanghai": 8, "Asia/Hong_Kong": 8, "Asia/Tokyo": 9,
    "Asia/Singapore": 8, "Europe/London": 0, "America/New_York": -5, "UTC": 0,
}


def timezone_name(db: Session) -> str:
    return str(read_value(db, "operations").get("timezone") or DEFAULT_TIMEZONE)


def timezone_for_name(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=TIMEZONE_OFFSETS.get(name, 8)), name=name)


def site_timezone(db: Session):
    return timezone_for_name(timezone_name(db))


def monitor_config(db: Session, config=None) -> dict:
    saved = read_value(db, "operations")
    return {
        "enabled": bool(saved.get("link_check_enabled", False)),
        "mode": saved.get("link_check_mode", "interval"),
        "interval_minutes": int(saved.get("link_check_interval_minutes", 360)),
        "daily_time": saved.get("link_check_daily_time", "03:00"),
        "batch_size": int(saved.get("link_check_batch_size", 50)),
        "timezone": saved.get("timezone", DEFAULT_TIMEZONE),
    }


def validate_operations(data) -> dict:
    zone = str(data.get("timezone", DEFAULT_TIMEZONE)).strip()
    if zone not in {value for value, _ in TIMEZONE_CHOICES}:
        raise ValueError("请选择有效的网站时区")
    mode = str(data.get("link_check_mode", "interval"))
    if mode not in {"interval", "daily"}:
        raise ValueError("请选择定时检测方式")
    try:
        interval = int(str(data.get("link_check_interval_minutes", "360")))
        batch = int(str(data.get("link_check_batch_size", "50")))
        hour, minute = map(int, str(data.get("link_check_daily_time", "03:00")).split(":"))
        time(hour, minute)
    except (TypeError, ValueError):
        raise ValueError("检测间隔、批量数量或固定时间格式不正确")
    if not 5 <= interval <= 10080:
        raise ValueError("检测间隔须为5—10080分钟")
    if not 1 <= batch <= 500:
        raise ValueError("每批检测数量须为1—500条")
    return {
        "timezone": zone,
        "link_check_enabled": data.get("link_check_enabled") == "yes",
        "link_check_mode": mode,
        "link_check_interval_minutes": interval,
        "link_check_daily_time": f"{hour:02d}:{minute:02d}",
        "link_check_batch_size": batch,
    }


def due_cutoff(db: Session, now: datetime | None = None) -> datetime | None:
    cfg = monitor_config(db)
    if not cfg["enabled"]:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if cfg["mode"] == "interval":
        return current - timedelta(minutes=cfg["interval_minutes"])
    local = current.astimezone(site_timezone(db))
    hour, minute = map(int, cfg["daily_time"].split(":"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < scheduled:
        return None
    return scheduled.astimezone(timezone.utc)
