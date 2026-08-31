from __future__ import annotations


STATUS_LABELS = {
    "draft": "草稿",
    "published": "已发布",
    "archived": "已归档",
    "active": "正常",
    "pending": "待检测",
    "invalid": "已失效",
    "error": "检测异常",
    "disabled": "已停用",
    "preview": "待确认",
    "committed": "已导入",
    "ready": "可导入",
    "warning": "需人工确认",
    "duplicate_batch": "批次内重复",
    "duplicate_existing": "系统中已存在",
    "conflict": "链接归属冲突",
    "unmatched": "未匹配到图书",
    "noop": "字段已完整",
    "meta_preview": "待确认",
    "meta_committed": "已补全",
    "meta_partial": "部分补全",
}

PROVIDER_LABELS = {
    "baidu": "百度网盘",
    "quark": "夸克网盘",
}

RESOURCE_TYPE_LABELS = {
    "book": "图书",
    "tutorial": "教程",
    "course": "课程",
}

LANGUAGE_LABELS = {
    "zh": "中文",
    "zh-cn": "中文",
    "zho": "中文",
    "chi": "中文",
    "en": "英文",
    "en-us": "英文",
    "en-gb": "英文",
    "eng": "英文",
    "ja": "日文",
    "jpn": "日文",
    "ko": "韩文",
    "kor": "韩文",
}


def status_label(value: str | None) -> str:
    return STATUS_LABELS.get(value or "", value or "未知")


def provider_label(value: str | None) -> str:
    return PROVIDER_LABELS.get(value or "", "其他网盘" if value else "未知渠道")


def resource_type_label(value: str | None) -> str:
    return RESOURCE_TYPE_LABELS.get(value or "", "资源")


def language_label(value: str | None) -> str:
    normalized = (value or "").strip().casefold()
    return LANGUAGE_LABELS.get(normalized, value or "未知")
