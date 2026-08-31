from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

from app.providers.baidu import BaiduAdapter
from app.providers.base import ParsedShareLink, ProviderAdapter
from app.providers.quark import QuarkAdapter


class ProviderRegistry:
    def __init__(self, adapters: list[ProviderAdapter] | None = None) -> None:
        self.adapters = adapters or [BaiduAdapter(), QuarkAdapter()]

    def recognize(self, url: str, extract_code: str | None = None) -> ParsedShareLink:
        raw = (url or "").strip()
        if raw and "://" not in raw:
            raw = f"https://{raw}"
        hostname = urlsplit(raw).hostname
        if not hostname:
            raise ValueError("分享链接格式不正确")
        for adapter in self.adapters:
            if adapter.matches(hostname):
                return adapter.parse(raw, extract_code)
        raise ValueError("暂不支持该网盘；当前支持百度网盘和夸克网盘")

    def get(self, code: str) -> ProviderAdapter:
        for adapter in self.adapters:
            if adapter.code == code:
                return adapter
        raise KeyError(code)


registry = ProviderRegistry()


def url_hash(normalized_url: str) -> str:
    parts = urlsplit(normalized_url)
    # 提取码可能被改写或单独填写，但底层分享仍是同一条；去掉查询参数后再判重。
    identity_url = urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), "", ""))
    return hashlib.sha256(identity_url.encode("utf-8")).hexdigest()
