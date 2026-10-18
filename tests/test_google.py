"""Google backend against a fake Drive API (httpx.MockTransport)."""
import io
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from drivehub.backends import google
from drivehub.backends.base import BackendError, DriveItem, NotFound
from drivehub.backends.google import GoogleDriveBackend, exchange_code


class FakeGoogle:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.handlers = []
        self.tokens = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            self.tokens += 1
            return httpx.Response(200, json={"access_token": f"tok{self.tokens}", "expires_in": 3600})
        for match, fn in self.handlers:
            if match(request):
                return fn(request)
        return httpx.Response(404, json={"error": {"message": "File not found"}})

    def on(self, method, path_part, fn):
        self.handlers.append((lambda r: r.method == method and path_part in r.url.path, fn))


async def no_sleep(_):
    return None


def make(fake):
    return GoogleDriveBackend("cid", "secret", "refresh", transport=httpx.MockTransport(fake),
                              sleep=no_sleep)


async def test_quota_unlimited_and_limited():
    fake = FakeGoogle()
    fake.on("GET", "/about", lambda r: httpx.Response(200, json={
        "user": {"emailAddress": "me@gmail.com"}, "storageQuota": {"usage": "100"}}))
    b = make(fake)
    q = await b.quota()
    assert q.email == "me@gmail.com" and q.total is None and q.used == 100
    assert fake.requests[1].headers["authorization"] == "Bearer tok1"


async def test_list_pages_and_query():
    fake = FakeGoogle()

    def files(r):
        params = parse_qs(urlparse(str(r.url)).query)
        if "pageToken" not in params:
            return httpx.Response(200, json={"nextPageToken": "p2", "files": [
                {"id": "1", "name": "a", "mimeType": "text/plain", "size": "5", "parents": ["root"]}]})
        return httpx.Response(200, json={"files": [
            {"id": "2", "name": "b", "mimeType": google.FOLDER_MIME, "parents": ["root"]}]})
    fake.on("GET", "/drive/v3/files", files)
    b = make(fake)
    items = await b.list_children("root")
    assert [(i.id, i.is_folder, i.size) for i in items] == [("1", False, 5), ("2", True, None)]
    q = parse_qs(urlparse(str(fake.requests[1].url)).query)["q"][0]
    assert q.startswith("'root' in parents and trashed = false")


async def test_search_escapes_quotes():
    fake = FakeGoogle()
    fake.on("GET", "/drive/v3/files", lambda r: httpx.Response(200, json={"files": []}))
    await make(fake).search("it's a \\ test")
    q = parse_qs(urlparse(str(fake.requests[-1].url)).query)["q"][0]
    assert "name contains 'it\\'s a \\\\ test'" in q


async def test_retries_rate_limit_then_succeeds():
    fake = FakeGoogle()
    calls = {"n": 0}

    def about(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        if calls["n"] == 2:
            return httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})
        return httpx.Response(200, json={"user": {"emailAddress": "x"}, "storageQuota": {"limit": "10", "usage": "1"}})
    fake.on("GET", "/about", about)
    q = await make(fake).quota()
    assert q.total == 10 and calls["n"] == 3


async def test_gives_up_after_retries():
    fake = FakeGoogle()
    fake.on("GET", "/about", lambda r: httpx.Response(503))
    with pytest.raises(BackendError) as ei:
        await make(fake).quota()
    assert ei.value.status == 503


async def test_refreshes_token_once_on_401():
    fake = FakeGoogle()
    seen = []

    def about(r):
        seen.append(r.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"user": {"emailAddress": "x"}, "storageQuota": {"usage": "0"}})
    fake.on("GET", "/about", about)
    await make(fake).quota()
    assert seen == ["Bearer tok1", "Bearer tok2"]


async def test_not_found_and_permission_errors():
    fake = FakeGoogle()
    fake.on("PATCH", "/files/locked", lambda r: httpx.Response(403, json={
        "error": {"message": "insufficient permissions", "errors": [{"reason": "insufficientFilePermissions"}]}}))
    b = make(fake)
    with pytest.raises(NotFound):
        await b.get("missing")
    with pytest.raises(BackendError) as ei:
        await b.rename("locked", "x")
    assert ei.value.status == 403 and "insufficient" in str(ei.value)


async def test_resumable_upload_in_chunks(monkeypatch):
    monkeypatch.setattr(google, "CHUNK", 4)
    fake = FakeGoogle()
    received = bytearray()

    def start(r):
        meta = json.loads(r.content)
        assert meta == {"name": "f.bin", "parents": ["p1"]}
        assert r.headers["x-upload-content-length"] == "10"
        return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/abc"})

    def chunk(r):
        received.extend(r.content)
        start_, end_total = r.headers["content-range"][6:].split("-")
        end, total = end_total.split("/")
        if int(end) + 1 < int(total):
            return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})
        return httpx.Response(200, json={"id": "new", "name": "f.bin", "mimeType": "application/octet-stream",
                                         "size": total})
    fake.on("POST", "/upload/drive/v3/files", start)
    fake.on("PUT", "/upload/session/abc", chunk)
    item = await make(fake).upload("p1", "f.bin", io.BytesIO(b"0123456789"), 10, "application/octet-stream")
    assert item.id == "new" and bytes(received) == b"0123456789"


async def test_resumable_upload_recovers_from_server_error(monkeypatch):
    monkeypatch.setattr(google, "CHUNK", 4)
    fake = FakeGoogle()
    stored = bytearray()
    state = {"failed": False}
    fake.on("POST", "/upload/drive/v3/files",
            lambda r: httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/s"}))

    def put(r):
        cr = r.headers["content-range"]
        if cr.startswith("bytes */"):
            return httpx.Response(308, headers={"Range": f"bytes=0-{len(stored) - 1}"})
        if len(stored) == 4 and not state["failed"]:
            state["failed"] = True
            return httpx.Response(503)
        stored.extend(r.content)
        total = int(cr.split("/")[1])
        if len(stored) < total:
            return httpx.Response(308, headers={"Range": f"bytes=0-{len(stored) - 1}"})
        return httpx.Response(201, json={"id": "ok", "name": "x", "mimeType": "text/plain"})
    fake.on("PUT", "/upload/session/s", put)
    item = await make(fake).upload("p", "x", io.BytesIO(b"abcdefghij"), 10, "text/plain")
    assert item.id == "ok" and bytes(stored) == b"abcdefghij"


async def test_empty_file_upload():
    fake = FakeGoogle()
    fake.on("POST", "/upload/drive/v3/files",
            lambda r: httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/e"}))
    fake.on("PUT", "/upload/session/e", lambda r: httpx.Response(200, json={"id": "e", "name": "empty"}))
    item = await make(fake).upload("p", "empty", io.BytesIO(b""), 0, "text/plain")
    assert item.id == "e"
    assert fake.requests[-1].headers["content-range"] == "bytes */0"


async def test_workspace_doc_is_exported():
    fake = FakeGoogle()
    fake.on("GET", "/files/doc1/export", lambda r: httpx.Response(200, content=b"PK..", headers={"content-length": "4"}))
    b = make(fake)
    dl = await b.download(DriveItem("doc1", "Plan", False, mime="application/vnd.google-apps.document"))
    data = b"".join([c async for c in dl.chunks])
    await dl.aclose()
    assert data == b"PK.." and dl.mime.endswith("wordprocessingml.document")
    assert "mimeType=application" in str(fake.requests[-1].url)


async def test_range_download_passes_through():
    fake = FakeGoogle()
    fake.on("GET", "/files/f1", lambda r: httpx.Response(206, content=b"cd", headers={
        "content-range": "bytes 2-3/10", "content-length": "2"}))
    dl = await make(fake).download(DriveItem("f1", "x", False, mime="text/plain"), "bytes=2-3")
    assert dl.status == 206 and dl.content_range == "bytes 2-3/10"
    assert fake.requests[-1].headers["range"] == "bytes=2-3"
    await dl.aclose()


async def test_move_uses_add_and_remove_parents():
    fake = FakeGoogle()
    fake.on("PATCH", "/files/f1", lambda r: httpx.Response(200, json={"id": "f1", "name": "n", "mimeType": "text/plain"}))
    await make(fake).move("f1", "new", "old", "n")
    params = parse_qs(urlparse(str(fake.requests[-1].url)).query)
    assert params["addParents"] == ["new"] and params["removeParents"] == ["old"]


async def test_exchange_code_requires_refresh_token():
    def handler(r):
        body = parse_qs(r.content.decode())
        assert body["code_verifier"] == ["v"] and body["grant_type"] == ["authorization_code"]
        return httpx.Response(200, json={"access_token": "a"})
    with pytest.raises(BackendError):
        await exchange_code("c", "s", "http://x/cb", "code", "v", transport=httpx.MockTransport(handler))


async def test_revoked_refresh_token_is_reported():
    def handler(r):
        return httpx.Response(400, json={"error": "invalid_grant"})
    b = GoogleDriveBackend("c", "s", "r", transport=httpx.MockTransport(handler))
    with pytest.raises(BackendError) as ei:
        await b.quota()
    assert ei.value.status == 401 and "link it again" in str(ei.value)
