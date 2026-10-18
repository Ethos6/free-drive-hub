"""Storage backend interface.

A backend is one cloud account (one Google Drive). The hub merges any number of
backends into a single tree. Every method is async; implementations must be safe
to call concurrently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, BinaryIO, Optional, Protocol

FOLDER_MIME = "application/vnd.google-apps.folder"


@dataclass
class DriveItem:
    id: str
    name: str
    is_folder: bool
    size: Optional[int] = None
    mime: str = "application/octet-stream"
    modified: Optional[str] = None  # RFC 3339
    parents: list[str] = field(default_factory=list)
    web_link: Optional[str] = None
    md5: Optional[str] = None


@dataclass
class Quota:
    email: str
    total: Optional[int]  # None = unlimited
    used: int

    @property
    def free(self) -> int:
        if self.total is None:
            return 1 << 62
        return max(0, self.total - self.used)


@dataclass
class Download:
    """A streamed download. ``chunks`` yields bytes; call ``close`` when done."""

    chunks: AsyncIterator[bytes]
    size: Optional[int]
    mime: str
    status: int = 200  # 206 for a partial (Range) response
    content_range: Optional[str] = None
    close: Optional[object] = None

    async def aclose(self) -> None:
        if self.close is not None:
            await self.close()  # type: ignore[misc]


class BackendError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class NotFound(BackendError):
    def __init__(self, message: str = "not found"):
        super().__init__(message, 404)


class Backend(Protocol):
    async def quota(self) -> Quota: ...
    async def root_id(self) -> str: ...
    async def list_folders(self) -> list[DriveItem]: ...
    async def list_children(self, folder_id: str) -> list[DriveItem]: ...
    async def get(self, item_id: str) -> DriveItem: ...
    async def search(self, text: str, limit: int = 100) -> list[DriveItem]: ...
    async def download(self, item: DriveItem, range_header: Optional[str] = None) -> Download: ...
    async def upload(self, parent_id: str, name: str, data: BinaryIO, size: int, mime: str) -> DriveItem: ...
    async def update_content(self, item_id: str, data: BinaryIO, size: int, mime: str) -> DriveItem: ...
    async def mkdir(self, parent_id: str, name: str) -> DriveItem: ...
    async def rename(self, item_id: str, name: str) -> DriveItem: ...
    async def move(self, item_id: str, new_parent: str, old_parent: str, name: Optional[str] = None) -> DriveItem: ...
    async def copy(self, item_id: str, new_parent: str, name: str) -> DriveItem: ...
    async def trash(self, item_id: str) -> None: ...
    async def share(self, item_id: str) -> str: ...
    async def aclose(self) -> None: ...


# Google Workspace files have no binary content; they are exported on download.
EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}
DEFAULT_EXPORT = ("application/pdf", ".pdf")


def is_workspace_doc(mime: str) -> bool:
    return mime.startswith("application/vnd.google-apps.") and mime != FOLDER_MIME


def export_for(mime: str) -> tuple[str, str]:
    return EXPORTS.get(mime, DEFAULT_EXPORT)
