"""Linux 部署配置助手：只用标准库，不执行配置中的任何内容。"""
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets
import sys


def hostname(value: str) -> str:
    value = value.strip().lower()
    if not value or len(value) > 253 or "." not in value:
        raise ValueError("请输入完整域名，不带 https://、端口和路径")
    for label in value.split("."):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise ValueError("域名格式不正确，中文域名请填写转换后的 punycode 域名")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("正式部署请填写域名，不能填写 IP 地址")
    if value.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".home.arpa")):
        raise ValueError("正式部署不能使用本地域名或保留测试域名")
    return value


def read_config(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise ValueError("部署配置格式有误：每行应为唯一的 KEY=值")
        # 配置不需要 shell/Compose 插值、引号和行内注释。
        if any(c in value for c in "$`\"'\\#\x00"):
            raise ValueError(f"{key} 不允许插值、引号、反斜杠或行内注释")
        result[key] = value.strip()
    return result


def validate(values: dict[str, str]) -> None:
    hostname(values.get("SITE_DOMAIN", ""))
    aliases = [a for a in values.get("SITE_ALIASES", "").split(",") if a]
    if len(aliases) > 21:
        raise ValueError("最多20个别名，另外保留1个旧主域名")
    for alias in aliases:
        hostname(alias)
    for key in ("SESSION_SECRET", "MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "INITIAL_ADMIN_PASSWORD"):
        minimum = 48 if key == "SESSION_SECRET" else 24
        if not re.fullmatch(rf"[A-Za-z0-9_-]{{{minimum},128}}", values.get(key, "")):
            raise ValueError(f"{key} 应为至少 {minimum} 位随机字母、数字、下划线或连字符；请勿填写示例密码")
    for key in ("MYSQL_DATABASE", "MYSQL_USER", "ADMIN_USERNAME"):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", values.get(key, "")):
            raise ValueError(f"{key} 应为字母开头的名称，只含字母、数字、下划线，最长32位")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", values.get("COMPOSE_PROJECT_NAME", "")):
        raise ValueError("COMPOSE_PROJECT_NAME 格式错误")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", values.get("ORGANIZER_SITE_ID", "")):
        raise ValueError("ORGANIZER_SITE_ID 格式错误")
    if values.get("LINK_CHECK_AUTOMATIC_ENABLED") not in {"true", "false"}:
        raise ValueError("LINK_CHECK_AUTOMATIC_ENABLED 只能是 true 或 false")
    if not 1 <= len(values.get("APP_NAME", "")) <= 60:
        raise ValueError("APP_NAME 请输入1到60个字符的网站名称")
    for item in values.get("ORGANIZER_COVER_HOSTS", "").split(","):
        if item.strip():
            hostname(item)


def make_config(domain: str, app_name: str, admin: str) -> dict[str, str]:
    values = {
        "SITE_DOMAIN": hostname(domain),
        "APP_NAME": app_name.strip(),
        "COMPOSE_PROJECT_NAME": "ebook-site",
        "ADMIN_USERNAME": admin.strip(),
        "INITIAL_ADMIN_PASSWORD": secrets.token_hex(16),
        "SESSION_SECRET": secrets.token_hex(32),
        "MYSQL_DATABASE": "ebook_index",
        "MYSQL_USER": "ebook",
        "MYSQL_PASSWORD": secrets.token_hex(24),
        "MYSQL_ROOT_PASSWORD": secrets.token_hex(24),
        "ORGANIZER_SITE_ID": "site-" + secrets.token_hex(6),
        "ORGANIZER_COVER_HOSTS": "",
        "LINK_CHECK_AUTOMATIC_ENABLED": "false",
    }
    validate(values)
    return values


def create_config(path: Path, values: dict[str, str]) -> None:
    validate(values)
    for key, value in values.items():
        if any(c in value for c in "\r\n$`\"'\\#\x00"):
            raise ValueError(f"{key} 包含不支持的字符")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 排他创建：重跑部署不得重置数据库密码和网站编号。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("# 私密部署配置。禁止上传 GitHub。不要改变已有数据库密码或项目名称。\n")
        stream.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "check", "domain"])
    parser.add_argument("--config", type=Path, default=Path("deploy/.env"))
    parser.add_argument("--domain")
    parser.add_argument("--name", default="书库")
    parser.add_argument("--admin", default="admin")
    args = parser.parse_args()
    try:
        if args.action == "init" and not args.config.exists():
            domain = args.domain or input("正式网站域名（不带 https://，现在不填可按 Ctrl+C 退出）：").strip()
            name = args.name if args.domain else input("网站名称 [书库]：").strip() or "书库"
            values = make_config(domain, name, args.admin)
            create_config(args.config, values)
            print(f"配置已生成：{args.config}")
            print(f"首次管理员：{values['ADMIN_USERNAME']}")
            print(f"首次密码：{values['INITIAL_ADMIN_PASSWORD']}（请保存到密码管理器，不要截图公开）")
        values = read_config(args.config)
        validate(values)
        if args.action == "domain":
            print(values["SITE_DOMAIN"])
        else:
            print("正式配置检查通过；已有配置不会被重置。")
    except (ValueError, OSError, EOFError) as exc:
        print(f"配置未通过：{exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
