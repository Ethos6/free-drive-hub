"""In-memory backend used by the test suite and by ``drivehub demo``.

It mimics the parts of Google Drive behaviour the hub relies on: items have ids
and parent ids, names may repeat inside a folder, trash hides items, and each
account has a storage quota.
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
from datetime import datetime, timezone
from typing import AsyncIterator, BinaryIO, Optional

from .base import (FOLDER_MIME, BackendError, Download, DriveItem, NotFound, Quota,
                   export_for, is_workspace_doc)

_ids = itertools.count(1)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class MemoryBackend:
    def __init__(self, email: str, total: Optional[int] = 15 * 1024**3):
        self.email = email
        self.total = total
        self._root = f"root-{next(_ids)}"
        self.items: dict[str, DriveItem] = {
            self._root: DriveItem(self._root, "My Drive", True, mime=FOLDER_MIME)}
        self.data: dict[str, bytes] = {}
        self.trashed: set[str] = set()
        self.shared: set[str] = set()
        self.calls: list[str] = []

    # -- helpers for tests and demo data -----------------------------------
    def add_folder(self, parent: str, name: str) -> str:
        item = DriveItem(f"d{next(_ids)}", name, True, mime=FOLDER_MIME, parents=[parent],
                         modified=_now())
        self.items[item.id] = item
        return item.id

    def add_file(self, parent: str, name: str, content: bytes,
                 mime: str = "application/octet-stream") -> str:
        size = None if is_workspace_doc(mime) else len(content)
        item = DriveItem(f"f{next(_ids)}", name, False, size=size, mime=mime, parents=[parent],
                         modified=_now(), md5=hashlib.md5(content).hexdigest(),
                         web_link=f"https://drive.example/{name}")
        self.items[item.id] = item
        self.data[item.id] = content
        return item.id

    def _live(self, item_id: str) -> DriveItem:
        item = self.items.get(item_id)
        if item is None or item_id in self.trashed:
            raise NotFound(f"{item_id} not found")
        return item

    def _used(self) -> int:
        return sum(len(v) for k, v in self.data.items() if k not in self.trashed)

    def _ensure_space(self, size: int) -> None:
        if self.total is not None and self._used() + size > self.total:
            raise BackendError("storageQuotaExceeded", 403)

    # -- Backend protocol ----------------------------------------------------
    async def quota(self) -> Quota:
        return Quota(self.email, self.total, self._used())

    async def root_id(self) -> str:
        return self._root

    async def list_folders(self) -> list[DriveItem]:
        self.calls.append("list_folders")
        return [i for k, i in self.items.items()
                if i.is_folder and k != self._root and k not in self.trashed]

    async def list_children(self, folder_id: str) -> list[DriveItem]:
        self.calls.append(f"list_children:{folder_id}")
        self._live(folder_id)
        return [i for k, i in self.items.items() if folder_id in i.parents and k not in self.trashed]

    async def get(self, item_id: str) -> DriveItem:
        return self._live(item_id)

    async def search(self, text: str, limit: int = 100) -> list[DriveItem]:
        t = text.lower()
        return [i for k, i in self.items.items()
                if k != self._root and k not in self.trashed and t in i.name.lower()][:limit]

    async def download(self, item: DriveItem, range_header: Optional[str] = None) -> Download:
        self._live(item.id)
        content = self.data[item.id]
        mime = item.mime
        if is_workspace_doc(item.mime):
            mime = export_for(item.mime)[0]
        status, content_range, body = 200, None, content
        if range_header and range_header.startswith("bytes="):
            start_s, _, end_s = range_header[6:].partition("-")
            start = int(start_s or 0)
            end = int(end_s) if end_s else len(content) - 1
            body = content[start:end + 1]
            status, content_range = 206, f"bytes {start}-{start + len(body) - 1}/{len(content)}"

        async def gen() -> AsyncIterator[bytes]:
            for i in range(0, len(body), 65536):
                yield body[i:i + 65536]
                await asyncio.sleep(0)
        return Download(gen(), len(body), mime, status, content_range)

    async def upload(self, parent_id: str, name: str, data: BinaryIO, size: int,
                     mime: str) -> DriveItem:
        self._live(parent_id)
        self._ensure_space(size)
        return self.items[self.add_file(parent_id, name, data.read(), mime)]

    async def update_content(self, item_id: str, data: BinaryIO, size: int,
                             mime: str) -> DriveItem:
        item = self._live(item_id)
        old = len(self.data.get(item_id, b""))
        self._ensure_space(max(0, size - old))
        content = data.read()
        self.data[item_id] = content
        item.size, item.modified = len(content), _now()
        item.md5 = hashlib.md5(content).hexdigest()
        return item

    async def mkdir(self, parent_id: str, name: str) -> DriveItem:
        self._live(parent_id)
        return self.items[self.add_folder(parent_id, name)]

    async def rename(self, item_id: str, name: str) -> DriveItem:
        item = self._live(item_id)
        item.name = name
        return item

    async def move(self, item_id: str, new_parent: str, old_parent: str,
                   name: Optional[str] = None) -> DriveItem:
        item = self._live(item_id)
        self._live(new_parent)
        item.parents = [p for p in item.parents if p != old_parent] + [new_parent]
        if name:
            item.name = name
        return item

    async def copy(self, item_id: str, new_parent: str, name: str) -> DriveItem:
        item = self._live(item_id)
        if item.is_folder:
            raise BackendError("cannot copy folders", 400)
        self._ensure_space(len(self.data[item_id]))
        return self.items[self.add_file(new_parent, name, self.data[item_id], item.mime)]

    async def trash(self, item_id: str) -> None:
        self._live(item_id)
        stack = [item_id]
        while stack:
            cur = stack.pop()
            self.trashed.add(cur)
            stack.extend(k for k, i in self.items.items() if cur in i.parents)

    async def share(self, item_id: str) -> str:
        self._live(item_id)
        self.shared.add(item_id)
        return f"https://drive.example/share/{item_id}"

    async def aclose(self) -> None:
        return None
