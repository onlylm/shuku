from __future__ import annotations

import posixpath
import re
import stat
import time
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from .safeio import Cancelled, Control, bounded_read


class EpubError(ValueError):
    def __init__(self, code: str, message: str, blocked: bool = False):
        super().__init__(message)
        self.code, self.blocked = code, blocked


@dataclass
class Limits:
    file_bytes: int = 2 * 1024**3
    expanded_bytes: int = 4 * 1024**3
    member_bytes: int = 256 * 1024**2
    entries: int = 100_000
    ratio: int = 200
    seconds: float = 120


@dataclass
class Inspection:
    metadata: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    cover: bytes | None = None
    external_opf: bytes | None = None
    status: str = "passed"

    def warn(self, code, message):
        self.issues.append({"code": code, "message": message})
        if self.status == "passed":
            self.status = "warning"


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value: str) -> str:
    parser = PlainText()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())


def isbn_valid(value: str) -> str | None:
    clean = re.sub(r"[\s-]", "", value).upper()
    clean = re.sub(r"^(?:URN:ISBN:|ISBN(?:1[03])?:?)", "", clean)
    if re.fullmatch(r"\d{9}[\dX]", clean):
        if sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(clean)) % 11 == 0:
            return clean
    if re.fullmatch(r"97[89]\d{10}", clean):
        if sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(clean)) % 10 == 0:
            return clean
    return None


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def xml(data: bytes):
    if len(data) > 4 * 1024**2:
        raise EpubError("LIMIT_EXCEEDED", "XML 文档过大", True)
    # 允许旧 EPUB 的惰性 DOCTYPE 声明；不加载外部 DTD，不展开自定义实体。
    return ET.fromstring(data, forbid_dtd=False, forbid_entities=True, forbid_external=True)


def parse_metadata(package) -> tuple[dict, list[str]]:
    metadata = next((n for n in package if local_name(n.tag) == "metadata"), None)
    if metadata is None:
        return {}, []
    values: dict[str, list[str]] = {}
    for node in metadata:
        text = "".join(node.itertext()).strip()
        if text:
            values.setdefault(local_name(node.tag), []).append(text)
    first = lambda name: values.get(name, [""])[0]
    candidates = set()
    invalid = []
    for value in values.get("identifier", []):
        valid = isbn_valid(value)
        if valid:
            candidates.add(valid)
        elif "isbn" in value.lower() or re.fullmatch(r"[\dXx\s-]{10,20}", value):
            invalid.append(value)
    year = re.match(r"(\d{4})", first("date"))
    result = {
        "title": first("title"), "author": " / ".join(values.get("creator", [])),
        "authors": values.get("creator", []), "publisher": first("publisher"),
        "description": plain_text(first("description")), "language": first("language") or "zh-CN",
        "publish_year": int(year[1]) if year else None,
        "isbn": next(iter(candidates)) if len(candidates) == 1 else "",
        "isbn_candidates": sorted(candidates), "isbn_invalid": invalid,
        "subjects": values.get("subject", []), "formats": "EPUB",
    }
    translators = []
    for node in metadata:
        role = next((v for k, v in node.attrib.items() if local_name(k) == "role"), "")
        if role == "trl":
            translators.append("".join(node.itertext()).strip())
        if local_name(node.tag) == "meta" and node.attrib.get("property") == "title-type" and (node.text or "").strip() == "subtitle":
            title_id = node.attrib.get("refines", "").lstrip("#")
            result["subtitle"] = next(("".join(n.itertext()).strip() for n in metadata if n.attrib.get("id") == title_id), "")
    if translators:
        result["translator"] = " / ".join(translators)
        result["authors"] = [name for name in result["authors"] if name not in translators]
        result["author"] = " / ".join(result["authors"])
    return result, invalid


def resolve_member(base: str, href: str) -> str | None:
    uri = urlsplit(href)
    if uri.scheme or uri.netloc:
        return None
    path = unquote(uri.path)
    if "\\" in path or path.startswith("/") or "\x00" in path:
        raise EpubError("UNSAFE_PATH", "EPUB 引用包含危险路径", True)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    if target == ".." or target.startswith("../"):
        raise EpubError("UNSAFE_PATH", "EPUB 引用越出文件边界", True)
    return target


def inspect_epub(path: Path, control: Control | None = None, limits: Limits | None = None) -> Inspection:
    result = Inspection()
    limits, control = limits or Limits(), control or Control()
    started = time.monotonic()
    try:
        if not path.stat().st_size:
            raise EpubError("EMPTY_FILE", "文件为空")
        if path.stat().st_size > limits.file_bytes:
            raise EpubError("LIMIT_EXCEEDED", "EPUB 超过单文件限制", True)
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > limits.entries:
                raise EpubError("LIMIT_EXCEEDED", "ZIP 成员过多", True)
            names = set()
            total = 0
            for member in members:
                control.check()
                name = member.filename
                normalized = posixpath.normpath(name)
                if (name.startswith("/") or "\\" in name or normalized.startswith("../")
                    or normalized == ".." or ":" in name or normalized in names
                    or stat.S_ISLNK(member.external_attr >> 16)):
                    raise EpubError("UNSAFE_PATH", "ZIP 路径不安全或成员重复", True)
                names.add(normalized)
                if member.flag_bits & 1:
                    raise EpubError("EPUB_ENCRYPTED", "ZIP 内容已加密，不自动处理", True)
                if member.is_dir():
                    continue
                if member.file_size > limits.member_bytes or member.file_size / max(member.compress_size, 1) > limits.ratio:
                    raise EpubError("LIMIT_EXCEEDED", "ZIP 成员或压缩比例超限", True)
                with archive.open(member) as stream:
                    while chunk := stream.read(256 * 1024):
                        control.check()
                        total += len(chunk)
                        if total > limits.expanded_bytes or time.monotonic() - started > limits.seconds:
                            raise EpubError("LIMIT_EXCEEDED", "展开数据或检测时间超限", True)
            if "mimetype" not in names or archive.read("mimetype").strip() != b"application/epub+zip":
                raise EpubError("EPUB_CONTAINER_INVALID", "缺少有效 EPUB mimetype")
            if members[0].filename != "mimetype" or archive.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
                result.warn("MIMETYPE_LAYOUT", "mimetype 排序或压缩方式不规范")
            container = xml(archive.read("META-INF/container.xml"))
            roots = [n.attrib["full-path"] for n in container.iter() if local_name(n.tag) == "rootfile"]
            if len(roots) != 1:
                raise EpubError("UNSUPPORTED_RENDITION", "多版本包或缺少包文档，需要人工处理", True)
            opf_path = resolve_member("root", roots[0])
            package = xml(archive.read(opf_path))
            result.metadata, invalid = parse_metadata(package)
            result.provenance = {key: "EPUB 内部 OPF" for key, value in result.metadata.items() if value}
            if "META-INF/encryption.xml" in names:
                encryption = xml(archive.read("META-INF/encryption.xml"))
                allowed = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
                algorithms = [n.attrib.get("Algorithm") for n in encryption.iter() if local_name(n.tag) == "EncryptionMethod"]
                if not algorithms or any(a not in allowed for a in algorithms):
                    raise EpubError("EPUB_ENCRYPTED", "含不支持的加密内容", True)
                references = [n.attrib.get("URI", "") for n in encryption.iter() if local_name(n.tag) == "CipherReference"]
                if not references or any(not r or not resolve_member("root", r) or Path(urlsplit(r).path).suffix.lower() not in {".otf", ".ttf", ".woff", ".woff2"} for r in references):
                    raise EpubError("EPUB_ENCRYPTED", "字体混淆声明指向非字体或缺少引用，需要人工检查", True)
                result.warn("FONT_OBFUSCATION", "检测到字体混淆，未当作正文加密")
            manifest = {n.attrib["id"]: n for n in package.iter() if local_name(n.tag) == "item" and "id" in n.attrib}
            spine = [n.attrib.get("idref") for n in package.iter() if local_name(n.tag) == "itemref"]
            if not spine:
                raise EpubError("EPUB_BODY_MISSING", "缺少正文阅读顺序")
            for reference in spine:
                if reference not in manifest:
                    raise EpubError("EPUB_BODY_MISSING", "正文引用不在资源清单中")
                target = resolve_member(opf_path, manifest[reference].attrib.get("href", ""))
                if not target or target not in names or not archive.getinfo(target).file_size:
                    raise EpubError("EPUB_BODY_MISSING", "正文文件不存在或为空")
                body = xml(archive.read(target))
                for node in body.iter():
                    for attribute in ("src", "href"):
                        href = node.attrib.get(attribute)
                        if href and urlsplit(href).path:
                            dependency = resolve_member(target, href)
                            if dependency and dependency not in names:
                                if local_name(node.tag) == "a" and attribute == "href":
                                    if not any(i["code"] == "BROKEN_HYPERLINK" for i in result.issues):
                                        result.warn("BROKEN_HYPERLINK", "正文有失效的本地超链接，正文仍可读取")
                                else:
                                    raise EpubError("EPUB_DEPENDENCY_MISSING", "正文引用的关键本地文件缺失")
            for node in manifest.values():
                target = resolve_member(opf_path, node.attrib.get("href", ""))
                if target and target not in names:
                    raise EpubError("EPUB_DEPENDENCY_MISSING", "资源清单中的本地文件缺失")
            cover_ids = {n.attrib.get("content") for n in package.iter() if local_name(n.tag) == "meta" and n.attrib.get("name") == "cover"}
            candidates = [n for key, n in manifest.items() if key in cover_ids or "cover-image" in n.attrib.get("properties", "").split()]
            if len(candidates) == 1:
                cover_path = resolve_member(opf_path, candidates[0].attrib.get("href", ""))
                if cover_path and archive.getinfo(cover_path).file_size <= 20 * 1024**2:
                    result.cover = archive.read(cover_path)
                    result.provenance["cover"] = "EPUB 显式封面"
            elif len(candidates) > 1:
                result.warn("COVER_AMBIGUOUS", "内部存在多个封面声明")
        siblings = [p for p in path.parent.iterdir() if p.suffix.lower() == ".epub"]
        external = [p for p in path.parent.iterdir() if p.suffix.lower() == ".opf"]
        matched = False
        if len(siblings) == 1 and len(external) == 1:
            try:
                raw = bounded_read(external[0], 4 * 1024**2)
                data, _ = parse_metadata(xml(raw))
                title_key = lambda x: re.sub(r"\W", "", x).casefold()
                matched = bool(data.get("title")) and (not result.metadata.get("title") or title_key(data["title"]) == title_key(result.metadata["title"]))
                result.external_opf = raw
                if matched:
                    for key, value in data.items():
                        if value:
                            result.metadata[key] = value
                            result.provenance[key] = "同目录匹配 OPF"
                else:
                    result.warn("METADATA_CONFLICT", "外部 OPF 与书内标题不一致，未覆盖")
            except Exception:
                result.warn("EXTERNAL_OPF_INVALID", "外部 OPF 无法安全读取，使用书内资料")
        elif external:
            result.warn("ATTACHMENT_AMBIGUOUS", "多本书或多个 OPF，外部附件未自动绑定")
        covers = [p for p in path.parent.iterdir() if p.stem.lower() == "cover" and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
        if len(siblings) == 1 and len(covers) == 1 and matched:
            try:
                result.cover = bounded_read(covers[0], 20 * 1024**2)
                result.provenance["cover"] = "同目录匹配封面"
            except Exception:
                result.warn("EXTERNAL_COVER_INVALID", "外部封面不可读取，保留书内封面")
        if not result.metadata.get("title"):
            parts = path.stem.split(" - ", 1)
            result.metadata.update(title=parts[0], author=parts[1] if len(parts) > 1 else "")
            result.provenance["title"] = "文件名推断（待确认）"
            result.warn("TITLE_INFERRED", "缺少书名，使用文件名推断，请确认")
        if not result.cover:
            result.warn("COVER_MISSING", "未找到明确封面，可人工选择")
        if not result.metadata.get("isbn"):
            result.warn("ISBN_MISSING", "ISBN 缺失或存在多个候选，不自动猜测版本")
        if result.metadata.get("isbn_invalid"):
            result.warn("ISBN_INVALID", "资料包含校验不通过的 ISBN")
    except Cancelled:
        raise
    except EpubError as exc:
        result.status = "blocked" if exc.blocked else "failed"
        result.issues.append({"code": exc.code, "message": str(exc)})
    except DefusedXmlException:
        result.status = "blocked"
        result.issues.append({"code": "UNSAFE_XML", "message": "含实体声明或外部实体引用，已安全阻止"})
    except (zipfile.BadZipFile, KeyError, OSError, ValueError, RuntimeError, ET.ParseError) as exc:
        result.status = "failed"
        result.issues.append({"code": "EPUB_INVALID", "message": f"文件或 EPUB 结构无效：{type(exc).__name__}"})
    except Exception as exc:
        result.status = "blocked"
        result.issues.append({"code": "UNSAFE_XML", "message": f"不支持或不安全的内容：{type(exc).__name__}"})
    return result
