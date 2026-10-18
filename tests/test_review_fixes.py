"""Regression tests for issues found in code review."""
import base64

import httpx
import pytest

from drivehub.backends.base import BackendError
from drivehub.hub import HubError

from .conftest import MB, PASSWORD, blob
from .test_google import FakeGoogle, make

DOC = "application/vnd.google-apps.document"


# -- WebDAV must not act with the browser session --------------------------------------

def test_dav_ignores_session_cookie(client):
    assert client.get("/api/files").status_code == 200  # signed in through the web UI
    assert client.request("PROPFIND", "/dav/").status_code == 401
    assert client.get("/dav/").status_code == 401


def test_dav_files_are_sandboxed_downloads(client, backends):
    a, _ = backends
    a.add_file(a._root, "evil.html", b"<script>alert(1)</script>", "text/html")
    pw = client.post("/api/app-passwords", json={"name": "x"}).json()["password"]
    auth = {"Authorization": "Basic " + base64.b64encode(f"u:{pw}".encode()).decode()}
    client.cookies.clear()
    r = client.get("/dav/evil.html", headers=auth)
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-security-policy"].startswith("sandbox")
    listing = client.get("/dav/", headers=auth)
    assert listing.headers["content-security-policy"].startswith("sandbox")


def test_dav_destination_must_be_under_prefix(client):
    pw = client.post("/api/app-passwords", json={"name": "x"}).json()["password"]
    auth = {"Authorization": "Basic " + base64.b64encode(f"u:{pw}".encode()).decode()}
    client.cookies.clear()
    client.put("/dav/a.txt", content=b"a", headers=auth)
    r = client.request("MOVE", "/dav/a.txt",
                       headers={**auth, "Destination": "http://testserver/davX/a.txt"})
    assert r.status_code == 502


# -- hub ----------------------------------------------------------------------------------

async def test_google_doc_cannot_be_overwritten(hub, backends):
    a, _ = backends
    a.add_file(a._root, "Report", b"doc", DOC)
    with pytest.raises(HubError) as ei:
        await hub.write("/Report.docx", blob(10), 10)
    assert ei.value.status == 409
    assert [e.name for e in await hub.ls("/")] == ["Report.docx"]


async def test_same_named_folders_in_one_account_all_listed(hub, backends):
    a, _ = backends
    a.add_file(a.add_folder(a._root, "Photos"), "one.jpg", b"1")
    a.add_file(a.add_folder(a._root, "Photos"), "two.jpg", b"2")
    assert [e.name for e in await hub.ls("/Photos")] == ["one.jpg", "two.jpg"]
    await hub.move("/Photos/two.jpg", "/two.jpg")
    assert [e.name for e in await hub.ls("/Photos")] == ["one.jpg"]


async def test_overwrite_that_fits_nowhere_keeps_old_file(hub, backends):
    a, _ = backends
    fid = a.add_file(a._root, "big.bin", b"x" * (9 * MB))
    with pytest.raises(HubError) as ei:
        await hub.write("/big.bin", blob(25 * MB), 25 * MB)
    assert ei.value.status == 507
    assert fid not in a.trashed and (await hub.stat("/big.bin")).size == 9 * MB


async def test_overwrite_moves_to_roomier_account_first(hub, backends):
    a, b = backends
    fid = a.add_file(a._root, "grow.bin", b"x" * MB)
    await hub.write("/grow.bin", blob(12 * MB), 12 * MB)  # alpha only has 10 MB
    e = await hub.stat("/grow.bin")
    assert e.sources[0][0].label == "beta" and e.size == 12 * MB
    assert fid in a.trashed


async def test_moving_keeps_drive_names(hub, backends):
    a, b = backends
    doc = a.add_file(a._root, "Notes", b"doc", DOC)
    a.add_file(a._root, "r.txt", b"a")
    work = b.add_file(b._root, "r.txt", b"b")
    await hub.mkdir("/Dir")
    await hub.move("/Notes.docx", "/Dir/Notes.docx")
    assert a.items[doc].name == "Notes"
    await hub.move("/r (beta).txt", "/Dir/r (beta).txt")
    assert b.items[work].name == "r.txt"
    await hub.move("/Dir/Notes.docx", "/Dir/Plan.docx")
    assert a.items[doc].name == "Plan"


async def test_search_points_at_disambiguated_name(hub, backends):
    a, b = backends
    a.add_file(a._root, "r.txt", b"a")
    b.add_file(b._root, "r.txt", b"b")
    res = {(r["account"], r["path"]) for r in await hub.search("r.txt")}
    assert res == {("alpha", "/r.txt"), ("beta", "/r (beta).txt")}


async def test_transfer_stops_when_an_account_cannot_be_read(hub, backends):
    a, b = backends
    d = a.add_folder(a._root, "Keep")
    a.add_file(d, "1.txt", b"1")
    b.add_folder(b._root, "Keep")
    await hub.ls("/Keep")
    original = a.list_children

    async def flaky(folder_id):
        raise BackendError("temporary failure", 503)
    a.list_children = flaky
    with pytest.raises(HubError) as ei:
        await hub.transfer("/Keep", 2)
    assert ei.value.status == 503
    a.list_children = original
    assert d not in a.trashed


# -- proxy and throttle -------------------------------------------------------------------

def test_forwarded_for_uses_proxy_added_address(make_client, tmp_path):
    with make_client() as c:
        c.app.state.settings.trust_proxy = True
        for i in range(10):
            hdr = {"X-DriveHub": "1", "X-Forwarded-For": f"10.0.0.{i}, 203.0.113.5"}
            c.post("/api/login", json={"password": "bad"}, headers=hdr)
        hdr = {"X-DriveHub": "1", "X-Forwarded-For": "10.9.9.9, 203.0.113.5"}
        assert c.post("/api/login", json={"password": PASSWORD}, headers=hdr).status_code == 429


# -- Google client -----------------------------------------------------------------------

async def test_create_requests_are_not_retried_after_server_error():
    fake = FakeGoogle()
    calls = {"n": 0}

    def create(r):
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"message": "backend error"}})
    fake.on("POST", "/drive/v3/files", create)
    with pytest.raises(BackendError):
        await make(fake).mkdir("root", "New")
    assert calls["n"] == 1


async def test_empty_upload_error_is_reported():
    fake = FakeGoogle()
    fake.on("POST", "/upload/drive/v3/files",
            lambda r: httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/session/z"}))
    fake.on("PUT", "/upload/session/z", lambda r: httpx.Response(403, json={"error": {"message": "quota"}}))
    with pytest.raises(BackendError) as ei:
        import io
        await make(fake).upload("p", "e", io.BytesIO(b""), 0, "text/plain")
    assert ei.value.status == 403
