from __future__ import annotations

import re
import unicodedata


FORMAT_SUFFIX = re.compile(r"(?:[\s._-]*(?:pdf|epub|mobi|azw3|txt|zip|rar))+$", re.IGNORECASE)
BRACKETS = re.compile(r"[《》〈〉「」『』【】\[\]()（）{}]")
SPACES = re.compile(r"\s+")
NON_SLUG = re.compile(r"[^a-z0-9\u4e00-\u9fff]+", re.IGNORECASE)


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = FORMAT_SUFFIX.sub("", value)
    value = BRACKETS.sub("", value)
    value = SPACES.sub(" ", value)
    return value.casefold().strip()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    slug = NON_SLUG.sub("-", normalized).strip("-")
    return slug or "resource"


def clean_isbn(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^0-9xX]", "", str(value))
    return cleaned.upper() or None
