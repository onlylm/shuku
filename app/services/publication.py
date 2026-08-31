"""共享发布检查。检测不等同于法律核验，缺失事实不会被自动编造。"""
import re

from app.models.base import utcnow

RIGHTS = {"authorized", "public_domain", "open_license"}
POLLUTION = re.compile(r"https?://|www\.|公众号|微信号|加微信|关注.{0,12}(领取|获取)|扫码|免费领取|资源群|下载群|[Vv微][信xX][:：]|[Qq][Qq][:：]")


def publication_issues(resource) -> list[str]:
    issues = []
    if not (resource.title or "").strip():
        issues.append("书名不能为空")
    if resource.copyright_status not in RIGHTS:
        issues.append("版权状态待核验")
    if not (resource.source_reference or "").strip():
        issues.append("缺少授权或来源说明")
    for field, label in (("author", "作者"), ("publisher", "出版社")):
        if POLLUTION.search(getattr(resource, field, None) or ""):
            issues.append(f"{label}疑似含广告或联系方式，请核实")
    categories = list(resource.categories)
    visible = [c for c in categories if c.is_visible]
    roots = [c for c in visible if c.parent_id is None]
    children = [c for c in visible if c.parent_id is not None]
    if len(roots) != 1 or len(children) > 1 or (roots and any(c.parent_id != roots[0].id for c in children)):
        issues.append("请选择一个一级分类及至多一个对应二级分类")
    if len(visible) != len(categories):
        issues.append("分类已隐藏或合并，请重新选择")
    return issues


def apply_publication_gate(resource) -> list[str]:
    issues = publication_issues(resource)
    if resource.publish_status == "published":
        if issues:
            resource.publish_status = "draft"
        else:
            resource.published_at = resource.published_at or utcnow()
    return issues
