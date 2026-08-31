from app.providers.base import ProviderAdapter


class BaiduAdapter(ProviderAdapter):
    code = "baidu"
    name = "百度网盘"
    hostnames = ("pan.baidu.com",)

    def extract_share_id(self, path: str) -> str:
        segments = [item for item in path.split("/") if item]
        if len(segments) >= 2 and segments[-2] in {"s", "share"}:
            return segments[-1]
        return super().extract_share_id(path)

    def looks_available(self, status_code: int, body: str, final_url: str) -> tuple[bool, str]:
        ok, detail = super().looks_available(status_code, body, final_url)
        if ok and any(word in body for word in ("啊哦，你来晚了", "来晚啦")):
            return False, "分享已失效"
        return ok, detail
