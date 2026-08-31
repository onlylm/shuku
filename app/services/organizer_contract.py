"""桌面端与网站共用的版本化同步契约，不包含路径或凭据。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrganizerLink(StrictModel):
    url: str = Field(min_length=1, max_length=1000)
    extract_code: str | None = Field(default=None, max_length=20)


class OrganizerBook(StrictModel):
    book_id: str = Field(pattern=r"^BK_[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    epub_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=255)
    author: str | None = Field(default=None, max_length=255)
    translator: str | None = Field(default=None, max_length=255)
    publisher: str | None = Field(default=None, max_length=255)
    isbn: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=30000)
    language: str | None = Field(default="zh-CN", max_length=30)
    publish_year: int | None = Field(default=None, ge=1, le=9999)
    main_category: str | None = Field(default=None, max_length=100)
    subcategory: str | None = Field(default=None, max_length=100)
    rights_review_status: Literal["pending", "confirmed"] = "pending"
    copyright_status: Literal["", "authorized", "public_domain", "open_license"] | None = None
    source_reference: str | None = Field(default=None, max_length=500)
    cover_url: str | None = Field(default=None, max_length=500)
    formats: Literal["EPUB"] = "EPUB"
    links: list[OrganizerLink] = Field(default_factory=list, max_length=10)


class OrganizerPackage(StrictModel):
    schema_version: Literal["2.0"] = "2.0"
    site_id: str = Field(min_length=1, max_length=100)
    workspace_id: str = Field(min_length=1, max_length=100)
    export_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    books: list[OrganizerBook] = Field(min_length=1, max_length=500)


class CommitChoice(StrictModel):
    book_id: str
    action: Literal["create", "update", "bind"]
    resource_id: int | None = Field(default=None, ge=1)
    overwrite: bool = False
    publish: bool = False


class CommitChoices(StrictModel):
    choices: list[CommitChoice] = Field(min_length=1, max_length=500)
