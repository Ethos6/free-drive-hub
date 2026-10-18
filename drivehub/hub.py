"""The unified view: many drive accounts presented as one file tree.

Folders with the same path in different accounts are merged into one folder.
Files keep their names; when two files would clash in the merged view, the later
one gets the account label added, e.g. ``report (work).pdf``. New uploads go to the
account with the most free space, and the folder path is created there on demand.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Optional

from .backends.base import (Backend, BackendError, Download, DriveItem, NotFound, Quota,
                            export_for, is_workspace_doc)

log = logging.getLogger("drivehub")
SLASH_STANDIN = "∕"  # "∕" – Drive names may contain "/", paths may not


class HubError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def clean(name: str) -> str:
    return name.replace("/", SLASH_STANDIN).strip() or "_"


def real(name: str) -> str:
    return name.replace(SLASH_STANDIN, "/")


def display_name(item: DriveItem) -> str:
    name = clean(item.name)
    if not item.is_folder and is_workspace_doc(item.mime):
        ext = export_for(item.mime)[1]
        if not name.lower().endswith(ext):
            name += ext
    return name


def split(path: str) -> list[str]:
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise HubError("invalid path", 400)
    return parts


def join(parts: list[str]) -> str:
    return "/" + "/".join(parts)


@dataclass
class Account:
    id: int
    label: str
    email: str
    backend: Backend


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    size: Optional[int] = None
    modified: Optional[str] = None
    mime: str = "application/octet-stream"
    sources: list[tuple[Account, DriveItem]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        first = self.sources[0][1] if self.sources else None
        return {
            "name": self.name, "path": self.path, "is_dir": self.is_dir, "size": self.size,
            "modified": self.modified, "mime": self.mime,
            "accounts": sorted({a.label for a, _ in self.sources}),
            "google_doc": bool(first and is_workspace_doc(first.mime)),
            "web_link": first.web_link if first else None,
        }


class FolderIndex:
    """All folders of one account, so paths resolve without walking the API."""

    def __init__(self, root: str, folders: list[DriveItem]):
        self.root = root
        self.folders = {f.id: f for f in folders}
        self.children: dict[str, dict[str, list[str]]] = {}
        for f in folders:
            for p in f.parents:
                self.children.setdefault(p, {}).setdefault(clean(f.name), []).append(f.id)
        for names in self.children.values():
            for ids in names.values():
                ids.sort()
        self.built = time.monotonic()

    def resolve(self, parts: list[str]) -> Optional[str]:
        ids = self.resolve_all(parts)
        return ids[0] if ids else None

    def resolve_all(self, parts: list[str]) -> list[str]:
        """Every folder at this path; one account may hold same-named folders."""
        cur = [self.root]
        for part in parts:
            cur = [i for c in cur for i in self.children.get(c, {}).get(part, [])]
            if not cur:
                return []
        return cur

    def path_of(self, folder_id: str) -> Optional[list[str]]:
        parts: list[str] = []
        cur, seen = folder_id, set()
        while cur != self.root:
            f = self.folders.get(cur)
            if f is None or not f.parents or cur in seen:
                return None
            seen.add(cur)
            parts.append(clean(f.name))
            cur = f.parents[0]
        return parts[::-1]

    def add(self, item: DriveItem) -> None:
        self.folders[item.id] = item
        for p in item.parents:
            self.children.setdefault(p, {}).setdefault(clean(item.name), []).append(item.id)


class Hub:
    def __init__(self, accounts: Optional[list[Account]] = None, index_ttl: float = 300,
                 list_ttl: float = 15, quota_ttl: float = 30):
        self.accounts: list[Account] = accounts or []
        self.index_ttl, self.list_ttl, self.quota_ttl = index_ttl, list_ttl, quota_ttl
        self._index: dict[int, FolderIndex] = {}
        self._index_locks: dict[int, asyncio.Lock] = {}
        self._ls_cache: dict[str, tuple[float, list[Entry]]] = {}
        self._quota_cache: Optional[tuple[float, list[tuple[Account, Optional[Quota]]]]] = None
        self.errors: dict[int, str] = {}

    # -- account management ------------------------------------------------
    def set_accounts(self, accounts: list[Account]) -> None:
        self.accounts = accounts
        self.invalidate()

    def account(self, account_id: int) -> Account:
        for a in self.accounts:
            if a.id == account_id:
                return a
        raise HubError("unknown account", 404)

    def invalidate(self, index: bool = True) -> None:
        self._ls_cache.clear()
        self._quota_cache = None
        if index:
            self._index.clear()

    def _changed(self) -> None:
        self._ls_cache.clear()
        self._quota_cache = None

    async def _gather(self, coros: dict[int, Any]) -> dict[int, Any]:
        """Run one coroutine per account; a failing account is reported, not fatal."""
        ids = list(coros)
        results = await asyncio.gather(*coros.values(), return_exceptions=True)
        out = {}
        for aid, res in zip(ids, results):
            if isinstance(res, NotFound):
                continue
            if isinstance(res, Exception):
                self.errors[aid] = str(res)
                log.warning("account %s: %s", aid, res)
                continue
            self.errors.pop(aid, None)
            out[aid] = res
        return out

    async def index(self, acc: Account) -> FolderIndex:
        idx = self._index.get(acc.id)
        if idx and time.monotonic() - idx.built < self.index_ttl:
            return idx
        lock = self._index_locks.setdefault(acc.id, asyncio.Lock())
        async with lock:
            idx = self._index.get(acc.id)
            if idx and time.monotonic() - idx.built < self.index_ttl:
                return idx
            root, folders = await asyncio.gather(acc.backend.root_id(), acc.backend.list_folders())
            idx = self._index[acc.id] = FolderIndex(root, folders)
            return idx

    # -- storage -------------------------------------------------------------
    async def quotas(self) -> list[tuple[Account, Optional[Quota]]]:
        if self._quota_cache and time.monotonic() - self._quota_cache[0] < self.quota_ttl:
            return self._quota_cache[1]
        got = await self._gather({a.id: a.backend.quota() for a in self.accounts})
        res = [(a, got.get(a.id)) for a in self.accounts]
        self._quota_cache = (time.monotonic(), res)
        return res

    async def pick_account(self, size: int, prefer: Optional[list[Account]] = None) -> Account:
        """Account with the most free space that can hold ``size`` bytes."""
        ranked = sorted(((q.free, a) for a, q in await self.quotas() if q and q.free >= size),
                        key=lambda t: -t[0])
        if not ranked:
            raise HubError("not enough free space in any linked account", 507)
        if prefer:
            pref = [a for _, a in ranked if a in prefer]
            if pref:
                return pref[0]
        return ranked[0][1]

    # -- reading -------------------------------------------------------------
    async def _folder_ids(self, parts: list[str]) -> dict[int, list[str]]:
        async def one(acc: Account) -> list[str]:
            return (await self.index(acc)).resolve_all(parts)
        got = await self._gather({a.id: one(a) for a in self.accounts})
        return {aid: fids for aid, fids in got.items() if fids}

    async def _children(self, backend, folder_ids: list[str]) -> list[DriveItem]:
        lists = await asyncio.gather(*(backend.list_children(f) for f in folder_ids))
        return [item for lst in lists for item in lst]

    async def ls(self, path: str) -> list[Entry]:
        parts = split(path)
        key = join(parts)
        cached = self._ls_cache.get(key)
        if cached and time.monotonic() - cached[0] < self.list_ttl:
            return cached[1]
        folder_ids = await self._folder_ids(parts)
        if not folder_ids:
            if parts:
                raise HubError("folder not found", 404)
            return []
        listed = await self._gather({aid: self._children(self.account(aid).backend, fids)
                                     for aid, fids in folder_ids.items()})
        folders: dict[str, Entry] = {}
        files: list[tuple[Account, DriveItem]] = []
        for acc in self.accounts:
            for item in sorted(listed.get(acc.id, []), key=lambda i: (i.name, i.id)):
                if item.is_folder:
                    name = clean(item.name)
                    e = folders.get(name)
                    if e is None:
                        e = folders[name] = Entry(name, join(parts + [name]), True,
                                                  modified=item.modified, mime=item.mime)
                    e.sources.append((acc, item))
                    if item.modified and (e.modified or "") < item.modified:
                        e.modified = item.modified
                else:
                    files.append((acc, item))
        used = set(folders)
        entries = list(folders.values())
        for acc, item in files:
            name = _unique(display_name(item), acc.label, used)
            used.add(name)
            mime = export_for(item.mime)[0] if is_workspace_doc(item.mime) else item.mime
            entries.append(Entry(name, join(parts + [name]), False, item.size, item.modified,
                                 mime, [(acc, item)]))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        self._ls_cache[key] = (time.monotonic(), entries)
        return entries

    async def stat(self, path: str) -> Entry:
        parts = split(path)
        if not parts:
            return Entry("", "/", True, sources=[])
        for e in await self.ls(join(parts[:-1])):
            if e.name == parts[-1]:
                return e
        raise HubError("not found", 404)

    async def _find(self, parts: list[str], item_id: str) -> Entry:
        """The entry for a moved item; its shown name can differ from the requested one
        when a name clash is added or removed."""
        for e in await self.ls(join(parts[:-1])):
            if any(item.id == item_id for _, item in e.sources):
                return e
        return await self.stat(join(parts))

    async def exists(self, path: str) -> bool:
        try:
            await self.stat(path)
            return True
        except HubError as e:
            if e.status == 404:
                return False
            raise

    async def read(self, path: str, range_header: Optional[str] = None) -> tuple[Entry, Download]:
        e = await self.stat(path)
        if e.is_dir:
            raise HubError("is a folder", 400)
        acc, item = e.sources[0]
        try:
            return e, await acc.backend.download(item, range_header)
        except BackendError as err:
            raise HubError(str(err), err.status) from err

    async def search(self, text: str, limit: int = 200) -> list[dict]:
        text = text.strip()
        if not text:
            return []
        got = await self._gather({a.id: a.backend.search(text, limit) for a in self.accounts})
        out = []
        for acc in self.accounts:
            idx = self._index.get(acc.id) or await self.index(acc)
            for item in got.get(acc.id, []):
                parent = item.parents[0] if item.parents else None
                pparts = [] if parent == idx.root else (idx.path_of(parent) if parent else None)
                if pparts is None:
                    continue  # not inside this account's My Drive tree
                name = display_name(item)
                out.append({"name": name, "folder": join(pparts), "is_dir": item.is_folder,
                            "path": join(pparts + [name]), "size": item.size,
                            "modified": item.modified, "account": acc.label, "_id": item.id})
        await self._fix_search_paths(out)
        out.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
        return out[:limit]

    async def _fix_search_paths(self, results: list[dict], max_folders: int = 40) -> None:
        """A file whose name clashes in its folder is listed as e.g. ``a (work).txt``;
        look the folder up so search results open the right file."""
        folders = list(dict.fromkeys(r["folder"] for r in results))[:max_folders]
        listings = await asyncio.gather(*(self.ls(f) for f in folders), return_exceptions=True)
        by_item: dict[tuple[str, str], Entry] = {}
        for listing in listings:
            if isinstance(listing, BaseException):
                continue
            for e in listing:
                for acc, item in e.sources:
                    by_item[(acc.label, item.id)] = e
        for r in results:
            e = by_item.get((r["account"], r.pop("_id")))
            if e is not None:
                r["name"], r["path"] = e.name, e.path

    # -- writing -------------------------------------------------------------
    async def _ensure_dirs(self, acc: Account, parts: list[str]) -> str:
        idx = await self.index(acc)
        cur = idx.root
        for part in parts:
            ids = idx.children.get(cur, {}).get(part)
            if ids:
                cur = ids[0]
                continue
            created = await acc.backend.mkdir(cur, real(part))
            idx.add(created)
            cur = created.id
        return cur

    async def _parent_dir(self, parts: list[str]) -> Entry:
        parent = await self.stat(join(parts[:-1]))
        if not parent.is_dir:
            raise HubError("parent is not a folder", 409)
        return parent

    async def mkdir(self, path: str) -> Entry:
        parts = split(path)
        if not parts:
            raise HubError("cannot create the root", 400)
        parent = await self._parent_dir(parts)
        if await self.exists(path):
            raise HubError("already exists", 405)
        holders = [a for a, _ in parent.sources] if parts[:-1] else None
        acc = await self.pick_account(0, holders)
        await self._ensure_dirs(acc, parts)
        self._changed()
        return await self.stat(path)

    async def write(self, path: str, data: BinaryIO, size: int,
                    mime: Optional[str] = None) -> Entry:
        parts = split(path)
        if not parts:
            raise HubError("invalid file name", 400)
        await self._parent_dir(parts)
        mime = mime or mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
        try:
            existing = await self.stat(path)
        except HubError as e:
            if e.status != 404:
                raise
            existing = None
        try:
            if existing and existing.is_dir:
                raise HubError("a folder with that name exists", 409)
            if existing and is_workspace_doc(existing.sources[0][1].mime):
                raise HubError("this is a Google Docs file and cannot be overwritten; "
                               "save it under a different name", 409)
            if existing:
                acc, item = existing.sources[0]
                q = dict((a.id, q) for a, q in await self.quotas()).get(acc.id)
                if q is None or q.free + (item.size or 0) >= size:
                    await acc.backend.update_content(item.id, data, size, mime)
                    self._changed()
                    return await self.stat(path)
                # no longer fits its account: store the new version elsewhere first,
                # and only then remove the old one
                target = await self.pick_account(size)
                parent_id = await self._ensure_dirs(target, parts[:-1])
                await target.backend.upload(parent_id, item.name, data, size, mime)
                await acc.backend.trash(item.id)
                self._changed()
                for e in await self.ls(join(parts[:-1])):
                    if (not e.is_dir and e.sources[0][0].id == target.id
                            and e.sources[0][1].name == item.name):
                        return e
                return await self.stat(path)
            acc = await self.pick_account(size)
            parent_id = await self._ensure_dirs(acc, parts[:-1])
            await acc.backend.upload(parent_id, real(parts[-1]), data, size, mime)
        except BackendError as err:
            raise HubError(str(err), 507 if "quota" in str(err).lower() else err.status) from err
        finally:
            self._changed()
        return await self.stat(path)

    async def delete(self, path: str) -> None:
        if not split(path):
            raise HubError("cannot delete the root", 403)
        e = await self.stat(path)
        for acc, item in e.sources:
            await acc.backend.trash(item.id)
            if item.is_folder:
                self._index.pop(acc.id, None)
        self._changed()

    async def move(self, src: str, dst: str, overwrite: bool = False) -> Entry:
        sparts, dparts = split(src), split(dst)
        if not sparts or not dparts:
            raise HubError("cannot move the root", 403)
        if dparts[:len(sparts)] == sparts:
            raise HubError("cannot move a folder into itself", 409)
        e = await self.stat(src)
        await self._parent_dir(dparts)
        if join(sparts) == join(dparts):
            return e
        if await self.exists(dst):
            if not overwrite:
                raise HubError("destination exists", 412)
            await self.delete(dst)
        renamed = dparts[-1] != sparts[-1]
        for acc, item in e.sources:
            idx = await self.index(acc)
            src_parents = set(idx.resolve_all(sparts[:-1]))
            old_parent = next((p for p in item.parents if p in src_parents),
                              item.parents[0] if item.parents else idx.root)
            new_parent = await self._ensure_dirs(acc, dparts[:-1])
            new_name = _drive_name(item, dparts[-1]) if renamed else item.name
            if new_parent == old_parent:
                if new_name != item.name:
                    await acc.backend.rename(item.id, new_name)
            else:
                await acc.backend.move(item.id, new_parent, old_parent,
                                       new_name if new_name != item.name else None)
            if item.is_folder:
                self._index.pop(acc.id, None)
        self._changed()
        return await self._find(dparts, e.sources[0][1].id)

    async def copy(self, src: str, dst: str, overwrite: bool = False) -> Entry:
        sparts, dparts = split(src), split(dst)
        if not dparts or dparts[:len(sparts)] == sparts:
            raise HubError("invalid destination", 409)
        e = await self.stat(src)
        await self._parent_dir(dparts)
        if await self.exists(dst):
            if not overwrite:
                raise HubError("destination exists", 412)
            await self.delete(dst)
        if e.is_dir:
            children = await self._complete_ls(src, e)
            await self.mkdir(dst)
            for child in children:
                await self.copy(child.path, join(dparts + [child.name]))
            return await self.stat(dst)
        acc, item = e.sources[0]
        try:
            parent_id = await self._ensure_dirs(acc, dparts[:-1])
            await acc.backend.copy(item.id, parent_id, real(dparts[-1]))
        except BackendError as err:
            if err.status not in (403, 507):
                raise HubError(str(err), err.status) from err
            await self._stream_to(e, await self.pick_account(item.size or 0), dparts)
        self._changed()
        return await self.stat(dst)

    async def _complete_ls(self, path: str, e: Entry) -> list[Entry]:
        """List a folder, refusing to continue if any of its accounts failed to answer."""
        self._ls_cache.pop(join(split(path)), None)
        listing = await self.ls(path)
        # an account that failed may hold part of this folder, so stop rather than
        # act on a partial view
        failed = [a.label for a in self.accounts if a.id in self.errors]
        if failed:
            raise HubError(f"could not read {', '.join(failed)}; try again", 503)
        return listing

    async def _stream_to(self, e: Entry, target: Account, dparts: list[str],
                         keep_name: bool = False) -> None:
        acc, item = e.sources[0]
        dl = await acc.backend.download(item)
        with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as buf:
            try:
                async for chunk in dl.chunks:
                    buf.write(chunk)
            finally:
                await dl.aclose()
            size = buf.tell()
            buf.seek(0)
            parent_id = await self._ensure_dirs(target, dparts[:-1])
            if keep_name and not is_workspace_doc(item.mime):
                name = item.name
            else:
                name = real(dparts[-1])
            await target.backend.upload(parent_id, name, buf, size, dl.mime)

    async def transfer(self, path: str, account_id: int) -> Entry:
        """Move a file or folder into one specific account (e.g. to free space)."""
        target = self.account(account_id)
        e = await self.stat(path)
        parts = split(path)
        if not parts:
            raise HubError("cannot transfer the root", 403)
        if e.is_dir:
            await self._ensure_dirs(target, parts)
            for child in list(await self._complete_ls(path, e)):
                await self.transfer(child.path, account_id)
                self._changed()
            for acc, item in e.sources:
                if acc.id != target.id:
                    if await acc.backend.list_children(item.id):
                        raise HubError(f"some items in {acc.label} could not be moved; "
                                       "nothing was deleted there", 409)
                    await acc.backend.trash(item.id)
                    self._index.pop(acc.id, None)
        else:
            acc, item = e.sources[0]
            if acc.id == target.id:
                return e
            q = dict((a.id, q) for a, q in await self.quotas()).get(target.id)
            if q is not None and q.free < (item.size or 0):
                raise HubError(f"not enough free space in {target.label}", 507)
            await self._stream_to(e, target, parts, keep_name=True)
            await acc.backend.trash(item.id)
        self._changed()
        return await self.stat(path)

    async def share(self, path: str) -> list[dict]:
        e = await self.stat(path)
        if not e.sources:
            raise HubError("cannot share the root", 400)
        return [{"account": acc.label, "link": await acc.backend.share(item.id)}
                for acc, item in e.sources]


def _drive_name(item: DriveItem, shown: str) -> str:
    """Name to store in Drive for a renamed item. Google Docs keep no extension."""
    name = real(shown)
    if not item.is_folder and is_workspace_doc(item.mime):
        ext = export_for(item.mime)[1]
        if name.lower().endswith(ext) and len(name) > len(ext):
            name = name[: -len(ext)]
    return name


def _unique(name: str, label: str, used: set[str]) -> str:
    if name not in used:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    suffix = f".{ext}" if ext else ""
    candidate = f"{stem} ({label}){suffix}"
    n = 2
    while candidate in used:
        candidate = f"{stem} ({label} {n}){suffix}"
        n += 1
    return candidate
