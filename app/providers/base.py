from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


@dataclass(slots=True)
class ParsedShareLink:
    provider_code: str
    share_id: str
    normalized_url: str
    extract_code: str | None = None


class ProviderAdapter:
    code = "generic"
    name = "其他网盘"
    hostnames: tuple[str, ...] = ()

    def matches(self, hostname: str) -> bool:
        hostname = hostname.casefold().split(":", 1)[0]
        return any(hostname == host or hostname.endswith(f".{host}") for host in self.hostnames)

    def parse(self, url: str, extract_code: str | None = None) -> ParsedShareLink:
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname or not self.matches(parts.hostname):
            raise ValueError(f"不是有效的{self.name}分享链接")
        query = parse_qs(parts.query)
        code = (extract_code or query.get("pwd", [None])[0] or query.get("code", [None])[0])
        allowed_query = {}
        if code:
            allowed_query["pwd"] = str(code).strip()
        path = "/" + "/".join(segment for segment in parts.path.split("/") if segment)
        share_id = self.extract_share_id(path)
        normalized = urlunsplit(("https", parts.hostname.casefold(), path.rstrip("/"), urlencode(allowed_query), ""))
        return ParsedShareLink(self.code, share_id, normalized, str(code).strip() if code else None)

    def extract_share_id(self, path: str) -> str:
        segments = [item for item in path.split("/") if item]
        if not segments:
            raise ValueError(f"无法识别{self.name}分享标识")
        return segments[-1]

    def looks_available(self, status_code: int, body: str, final_url: str) -> tuple[bool, str]:
        if status_code >= 400:
            return False, f"HTTP {status_code}"
        lowered = body.casefold()
        failure_words = ("分享已失效", "链接不存在", "已被取消", "页面不存在", "文件已删除")
        for word in failure_words:
            if word in lowered:
                return False, word
        return True, "页面可访问"
