"""Google Drive backend (Drive API v3 over plain HTTPS with httpx).

Only the user's own "My Drive" is used. Access tokens are refreshed on demand
from the stored refresh token; requests are retried with exponential backoff on
rate limits and transient server errors.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import secrets
import time
from typing import Any, AsyncIterator, BinaryIO, Optional
from urllib.parse import urlencode

import httpx

from .base import (FOLDER_MIME, BackendError, Download, DriveItem, NotFound, Quota,
                   export_for, is_workspace_doc)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive"
FIELDS = "id,name,mimeType,size,modifiedTime,parents,webViewLink,md5Checksum"
SHORTCUT = "application/vnd.google-apps.shortcut"
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_TRIES = 6


# -- OAuth helpers ---------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def authorization_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


async def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str,
                        verifier: str, transport: Optional[httpx.AsyncBaseTransport] = None) -> dict:
    async with httpx.AsyncClient(transport=transport, timeout=30) as client:
        r = await client.post(TOKEN_URL, data={
            "client_id": client_id, "client_secret": client_secret, "code": code,
            "code_verifier": verifier, "grant_type": "authorization_code",
            "redirect_uri": redirect_uri})
    if r.status_code != 200:
        raise BackendError(f"Google token exchange failed: {r.text[:200]}", 400)
    data = r.json()
    if "refresh_token" not in data:
        raise BackendError("Google did not return a refresh token; remove the app's access at "
                           "myaccount.google.com/permissions and link again", 400)
    return data


def _q(value: str) -> str:
    """Escape a string literal for a Drive query."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _item(f: dict) -> DriveItem:
    mime = f.get("mimeType", "application/octet-stream")
    return DriveItem(
        id=f["id"], name=f.get("name", ""), is_folder=mime == FOLDER_MIME,
        size=int(f["size"]) if "size" in f else None, mime=mime,
        modified=f.get("modifiedTime"), parents=f.get("parents", []),
        web_link=f.get("webViewLink"), md5=f.get("md5Checksum"))


class GoogleDriveBackend:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 transport: Optional[httpx.AsyncBaseTransport] = None, sleep=asyncio.sleep):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60, connect=15),
                                         follow_redirects=True)
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._lock = asyncio.Lock()
        self._sleep = sleep
        self._root: Optional[str] = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ------------------------------------------------------------
    async def _access_token(self, force: bool = False) -> str:
        async with self._lock:
            if self._token and not force and time.time() < self._token_exp - 60:
                return self._token
            r = await self._client.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
            if r.status_code != 200:
                raise BackendError("Google rejected the saved sign-in for this account; "
                                   "link it again from the Accounts page", 401)
            data = r.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + int(data.get("expires_in", 3600))
            return self._token

    async def _request(self, method: str, url: str, *, stream: bool = False,
                       headers: Optional[dict] = None, retry_5xx: Optional[bool] = None,
                       **kw: Any) -> httpx.Response:
        # A POST that failed with a 5xx may still have created the folder/copy/permission,
        # so only retry it when Google said "slow down" (429 / rate limit), which means
        # nothing happened.
        if retry_5xx is None:
            retry_5xx = method != "POST"
        refreshed = False
        for attempt in range(MAX_TRIES):
            token = await self._access_token()
            h = {"Authorization": f"Bearer {token}", **(headers or {})}
            req = self._client.build_request(method, url, headers=h, **kw)
            r = await self._client.send(req, stream=stream)
            if r.status_code == 401 and not refreshed:
                await r.aclose()
                refreshed = True
                await self._access_token(force=True)
                continue
            slow_down = r.status_code == 429 or (r.status_code == 403 and await self._rate_limited(r))
            if slow_down or (retry_5xx and r.status_code in RETRY_STATUSES):
                await r.aclose()
                if attempt == MAX_TRIES - 1:
                    break
                await self._sleep(min(32, 2 ** attempt) + random.random())
                continue
            if r.status_code >= 400:
                if stream:
                    await r.aread()
                await r.aclose()
                if r.status_code == 404:
                    raise NotFound(_message(r))
                raise BackendError(_message(r), r.status_code)
            return r
        raise BackendError("Google Drive is rate limiting or unavailable; try again shortly", 503)

    @staticmethod
    async def _rate_limited(r: httpx.Response) -> bool:
        try:
            await r.aread()
            reasons = {e.get("reason") for e in r.json().get("error", {}).get("errors", [])}
        except Exception:
            return False
        return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})

    async def _json(self, method: str, url: str, **kw: Any) -> dict:
        r = await self._request(method, url, **kw)
        return r.json() if r.content else {}

    async def _list(self, q: str, limit: Optional[int] = None) -> list[DriveItem]:
        out: list[DriveItem] = []
        token = None
        while True:
            params = {"q": q, "fields": f"nextPageToken,files({FIELDS})",
                      "pageSize": 1000, "spaces": "drive"}
            if token:
                params["pageToken"] = token
            data = await self._json("GET", f"{API}/files", params=params)
            out.extend(_item(f) for f in data.get("files", []))
            token = data.get("nextPageToken")
            if not token or (limit and len(out) >= limit):
                return out[:limit] if limit else out

    # -- Backend protocol ----------------------------------------------------
    async def quota(self) -> Quota:
        data = await self._json("GET", f"{API}/about",
                                params={"fields": "user(emailAddress),storageQuota(limit,usage)"})
        sq = data.get("storageQuota", {})
        limit = sq.get("limit")
        return Quota(data.get("user", {}).get("emailAddress", ""),
                     int(limit) if limit else None, int(sq.get("usage", 0)))

    async def root_id(self) -> str:
        if self._root is None:
            self._root = (await self._json("GET", f"{API}/files/root", params={"fields": "id"}))["id"]
        return self._root

    async def list_folders(self) -> list[DriveItem]:
        return await self._list(f"mimeType = '{FOLDER_MIME}' and trashed = false")

    async def list_children(self, folder_id: str) -> list[DriveItem]:
        return await self._list(f"{_q(folder_id)} in parents and trashed = false "
                                f"and mimeType != '{SHORTCUT}'")

    async def get(self, item_id: str) -> DriveItem:
        return _item(await self._json("GET", f"{API}/files/{item_id}", params={"fields": FIELDS}))

    async def search(self, text: str, limit: int = 100) -> list[DriveItem]:
        return await self._list(f"name contains {_q(text)} and trashed = false "
                                f"and mimeType != '{SHORTCUT}' and 'me' in owners", limit)

    async def download(self, item: DriveItem, range_header: Optional[str] = None) -> Download:
        if is_workspace_doc(item.mime):
            mime = export_for(item.mime)[0]
            r = await self._request("GET", f"{API}/files/{item.id}/export",
                                    params={"mimeType": mime}, stream=True)
        else:
            headers = {"Range": range_header} if range_header else None
            r = await self._request("GET", f"{API}/files/{item.id}", params={"alt": "media"},
                                    headers=headers, stream=True)
            mime = item.mime
        length = r.headers.get("content-length")
        return Download(r.aiter_bytes(), int(length) if length else None, mime,
                        206 if r.status_code == 206 else 200, r.headers.get("content-range"),
                        r.aclose)

    async def _resumable(self, method: str, url: str, meta: dict, data: BinaryIO, size: int,
                         mime: str) -> DriveItem:
        # starting a session creates nothing yet, so it is safe to retry
        r = await self._request(method, url, params={"uploadType": "resumable", "fields": FIELDS},
                                json=meta, retry_5xx=True,
                                headers={"X-Upload-Content-Type": mime,
                                         "X-Upload-Content-Length": str(size)})
        session = r.headers.get("location")
        if not session:
            raise BackendError("Google did not start the upload", 502)
        if size == 0:
            r = await self._client.put(session, headers={"Content-Range": "bytes */0"}, content=b"")
            if r.status_code not in (200, 201):
                raise BackendError(_message(r), r.status_code)
            return _item(r.json())
        offset, failures = 0, 0
        while offset < size:
            data.seek(offset)
            chunk = data.read(CHUNK)
            end = offset + len(chunk) - 1
            try:
                r = await self._client.put(session, content=chunk, headers={
                    "Content-Range": f"bytes {offset}-{end}/{size}"})
            except httpx.TransportError:
                r = None
            if r is not None and r.status_code in (200, 201):
                return _item(r.json())
            if r is not None and r.status_code == 308:
                offset, failures = _next_offset(r, end + 1), 0
                continue
            if r is not None and r.status_code not in RETRY_STATUSES:
                raise BackendError(_message(r), r.status_code)
            failures += 1
            if failures >= MAX_TRIES:
                raise BackendError("upload failed after retries", 503)
            await self._sleep(min(32, 2 ** failures) + random.random())
            try:
                status = await self._client.put(session, headers={"Content-Range": f"bytes */{size}"})
            except httpx.TransportError:
                continue  # counts as another failure on the next round
            if status.status_code in (200, 201):
                return _item(status.json())
            if status.status_code == 308:
                offset = _next_offset(status, 0)
            elif status.status_code == 404:
                raise BackendError("upload session expired; try again", 503)
        raise BackendError("upload did not complete", 502)

    async def upload(self, parent_id: str, name: str, data: BinaryIO, size: int,
                     mime: str) -> DriveItem:
        return await self._resumable("POST", f"{UPLOAD_API}/files",
                                     {"name": name, "parents": [parent_id]}, data, size, mime)

    async def update_content(self, item_id: str, data: BinaryIO, size: int,
                             mime: str) -> DriveItem:
        return await self._resumable("PATCH", f"{UPLOAD_API}/files/{item_id}", {}, data, size, mime)

    async def mkdir(self, parent_id: str, name: str) -> DriveItem:
        return _item(await self._json("POST", f"{API}/files", params={"fields": FIELDS}, json={
            "name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}))

    async def rename(self, item_id: str, name: str) -> DriveItem:
        return _item(await self._json("PATCH", f"{API}/files/{item_id}",
                                      params={"fields": FIELDS}, json={"name": name}))

    async def move(self, item_id: str, new_parent: str, old_parent: str,
                   name: Optional[str] = None) -> DriveItem:
        return _item(await self._json(
            "PATCH", f"{API}/files/{item_id}", json={"name": name} if name else {},
            params={"addParents": new_parent, "removeParents": old_parent, "fields": FIELDS}))

    async def copy(self, item_id: str, new_parent: str, name: str) -> DriveItem:
        return _item(await self._json("POST", f"{API}/files/{item_id}/copy",
                                      params={"fields": FIELDS},
                                      json={"name": name, "parents": [new_parent]}))

    async def trash(self, item_id: str) -> None:
        await self._json("PATCH", f"{API}/files/{item_id}", json={"trashed": True},
                         params={"fields": "id"})

    async def share(self, item_id: str) -> str:
        await self._json("POST", f"{API}/files/{item_id}/permissions",
                         json={"role": "reader", "type": "anyone"}, params={"fields": "id"})
        return (await self.get(item_id)).web_link or f"https://drive.google.com/open?id={item_id}"


def _next_offset(r: httpx.Response, default: int) -> int:
    """Next byte to send after a 308 reply. No Range header means nothing was stored."""
    rng = r.headers.get("range")  # e.g. "bytes=0-8388607"
    if rng and "-" in rng:
        return int(rng.rsplit("-", 1)[1]) + 1
    return 0


def _message(r: httpx.Response) -> str:
    try:
        return json.loads(r.content).get("error", {}).get("message") or r.reason_phrase
    except Exception:
        return f"Google Drive error {r.status_code}"
