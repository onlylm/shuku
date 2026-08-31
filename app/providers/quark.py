from app.providers.base import ProviderAdapter


class QuarkAdapter(ProviderAdapter):
    code = "quark"
    name = "夸克网盘"
    hostnames = ("pan.quark.cn",)

    def extract_share_id(self, path: str) -> str:
        segments = [item for item in path.split("/") if item]
        if len(segments) >= 2 and segments[-2] == "s":
            return segments[-1]
        return super().extract_share_id(path)

    def looks_available(self, status_code: int, body: str, final_url: str) -> tuple[bool, str]:
        ok, detail = super().looks_available(status_code, body, final_url)
        if ok and any(word in body for word in ("该分享不存在", "分享已取消")):
            return False, "分享已失效"
        return ok, detail
