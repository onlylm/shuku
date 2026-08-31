"""只读检查公网 HTTPS，不创建资源，不触发真实网盘上传。"""
from __future__ import annotations

import argparse
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def check(base_url: str) -> None:
    for path in ("/api/v1/ready", "/", "/books", "/admin/login", "/robots.txt", "/sitemap.xml"):
        with urlopen(base_url.rstrip("/") + path, timeout=10) as response:
            if response.status != 200 or not response.url.startswith(base_url.rstrip("/") + "/"):
                raise ValueError(f"页面状态或跳转地址异常：{path}")
    print("HTTPS、数据库、前台、后台登录页、robots 和 sitemap 均响应正常。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--attempts", type=int, default=6)
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        raise SystemExit("正式验收必须使用 HTTPS，不跳过证书验证。")
    for attempt in range(max(1, args.attempts)):
        try:
            check(args.url)
            return
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            if attempt + 1 >= args.attempts:
                raise SystemExit(f"公网验收未通过：{exc}") from None
            time.sleep(5)


if __name__ == "__main__":
    main()
