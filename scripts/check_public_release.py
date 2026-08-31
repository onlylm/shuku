"""检查 Git 索引中的公开发布文件；只报告路径/行号，不打印疑似密钥。"""
from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath


FORBIDDEN_DIRS = {"runtime", "backups", "uploads", "screenshots", ".venv", "dist", "build", ".study", ".private", "prototype", "__pycache__"}
FORBIDDEN_PATHS = {
    "交接文档.md",
    "PROJECT-REQUIREMENTS.md",
    "DESIGN.md",
    "docs/PHASE-0-ACCEPTANCE.md",
    "docs/PHASE-1-ACCEPTANCE.md",
    "docs/PHASE-1-PLAN.md",
    "docs/UI-DESIGN-SYSTEM.md",
    "docs/后续开发与上线总计划.md",
    "docs/本地整理软件验收报告.md",
    "docs/桌面整理软件技术文档.md",
    "docs/桌面V8批量整理说明.md",
    "docs/桌面V8.1书名分类说明.md",
    "docs/桌面V9简介书单说明.md",
}
FORBIDDEN_SUFFIXES = {".db", ".sqlite3", ".epub", ".mobi", ".azw3", ".azw", ".pdf", ".docx", ".pem", ".key", ".pfx", ".pyc"}
PATTERNS = {
    "GitHub令牌": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})"),
    "Cloudflare令牌": re.compile(rb"\bcfat_[A-Za-z0-9_-]{20,}"),
    "AWS访问密钥": re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
    "私钥": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "疑似直接填写的密钥": re.compile(
        rb"(?i)(?:secret_access_key|api_key|access_token|refresh_token)\s*[=:]\s*[\"'][A-Za-z0-9_+/=-]{24,}[\"']"
    ),
}


def is_private_path(name: str) -> bool:
    """与忽略规则配合，拦截被强制暂存的运营数据、内部资料和旧原型。"""
    path = PurePosixPath(name)
    return bool(
        name in FORBIDDEN_PATHS
        or set(path.parts) & FORBIDDEN_DIRS
        or path.suffix.lower() in FORBIDDEN_SUFFIXES
        or (path.name.startswith(".env") and not path.name.endswith(".example"))
        or ".db-" in path.name or ".sqlite3-" in path.name
        or path.parts[:2] in {("samples", "organizer"), ("docs", "internal"), ("deploy", "control"), ("deploy", "domains")}
    )


def check() -> int:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode("utf-8").split("\0")
    problems = []
    count = total = 0
    for name in filter(None, paths):
        if is_private_path(name):
            problems.append(f"不应公开的文件：{name}")
            continue
        content = subprocess.check_output(["git", "show", ":" + name])
        count += 1
        total += len(content)
        if len(content) > 10 * 1024 * 1024:
            problems.append(f"文件异常偏大，请人工检查：{name}")
        for label, pattern in PATTERNS.items():
            match = pattern.search(content)
            if match:
                line = content[:match.start()].count(b"\n") + 1
                problems.append(f"{label}：{name}:{line}（内容已隐藏）")
    if problems:
        print("\n".join(problems))
        return 1
    print(f"公开发布检查通过：{count}个文件，{total}字节；未检出所列禁传文件或令牌模式。仍需人工审查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
