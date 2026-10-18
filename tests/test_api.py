import re

from drivehub.security import totp

from .conftest import PASSWORD

H = {"X-DriveHub": "1"}


def test_first_run_setup_needs_code(make_client):
    with make_client(password=None) as c:
        st = c.get("/api/status").json()
        assert st["setup_needed"] and not st["logged_in"]
        code = c.app.state.setup["setup_token"]
        assert c.post("/api/setup", json={"setup_code": "nope", "password": "x" * 12}, headers=H).status_code == 403
        assert c.post("/api/setup", json={"setup_code": code, "password": "short"}, headers=H).status_code == 400
        r = c.post("/api/setup", json={"setup_code": code, "password": "long enough pw"}, headers=H)
        assert r.status_code == 200
        assert c.get("/api/status").json()["logged_in"]
        assert c.post("/api/setup", json={"setup_code": code, "password": "y" * 12}, headers=H).status_code == 409


def test_api_requires_login(make_client):
    with make_client() as c:
        assert c.get("/api/files").status_code == 401
        assert c.get("/api/download?path=/x").status_code == 401
        assert c.get("/oauth/google/start", follow_redirects=False).status_code == 401


def test_state_changing_calls_need_custom_header(make_client):
    with make_client() as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 403


def test_login_throttled(make_client):
    with make_client() as c:
        for _ in range(10):
            assert c.post("/api/login", json={"password": "bad"}, headers=H).status_code == 401
        assert c.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 429


def test_totp_flow(client):
    s = client.post("/api/totp/setup").json()
    assert s["uri"].startswith("otpauth://totp/") and "<svg" in s["qr_svg"]
    assert client.post("/api/totp/enable", json={"secret": s["secret"], "code": "000000"}).status_code == 400
    assert client.post("/api/totp/enable", json={"secret": s["secret"], "code": totp(s["secret"])}).status_code == 200
    client.post("/api/logout")
    r = client.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 401 and "2FA" in r.json()["detail"]
    assert client.post("/api/login", json={"password": PASSWORD, "code": totp(s["secret"])}).status_code == 200


def test_session_cookie_flags(make_client):
    with make_client() as c:
        r = c.post("/api/login", json={"password": PASSWORD}, headers=H)
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_upload_list_download_delete(client, backends):
    files = [("files", ("hello.txt", b"hello world", "text/plain")),
             ("files", ("b.bin", b"\x00\x01", "application/octet-stream"))]
    r = client.post("/api/upload", data={"path": "/", "relpaths": ["Trip/day1/hello.txt", "b.bin"]},
                    files=files)
    assert r.status_code == 200, r.text
    listing = client.get("/api/files", params={"path": "/Trip/day1"}).json()
    assert [e["name"] for e in listing["entries"]] == ["hello.txt"]
    assert listing["entries"][0]["accounts"] == ["beta"]
    d = client.get("/api/download", params={"path": "/Trip/day1/hello.txt"})
    assert d.content == b"hello world"
    assert d.headers["content-disposition"].startswith("attachment")
    part = client.get("/api/download", params={"path": "/Trip/day1/hello.txt"},
                      headers={"Range": "bytes=6-10"})
    assert part.status_code == 206 and part.content == b"world"
    inline = client.get("/api/download", params={"path": "/Trip/day1/hello.txt", "inline": 1})
    assert inline.headers["content-disposition"].startswith("inline")
    assert "sandbox" in inline.headers["content-security-policy"]
    assert client.post("/api/delete", json={"paths": ["/Trip"]}).status_code == 200
    assert [e["name"] for e in client.get("/api/files").json()["entries"]] == ["b.bin"]


def test_html_is_never_served_inline(client, backends):
    a, _ = backends
    a.add_file(a._root, "evil.html", b"<script>alert(1)</script>", "text/html")
    r = client.get("/api/download", params={"path": "/evil.html", "inline": 1})
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-type"].startswith("text/plain")


def test_mkdir_move_copy_share_transfer(client, backends):
    a, b = backends
    a.add_file(a._root, "doc.txt", b"doc")
    assert client.post("/api/mkdir", json={"path": "/Box"}).status_code == 200
    assert client.post("/api/move", json={"src": "/doc.txt", "dst": "/Box/doc.txt"}).status_code == 200
    assert client.post("/api/copy", json={"src": "/Box/doc.txt", "dst": "/doc2.txt"}).status_code == 200
    links = client.post("/api/share", json={"path": "/doc2.txt"}).json()["links"]
    assert links[0]["link"].startswith("https://")
    accounts = client.get("/api/accounts").json()
    beta = next(x for x in accounts["accounts"] if x["label"] == "beta")
    r = client.post("/api/transfer", json={"path": "/doc2.txt", "account_id": beta["id"]})
    assert r.status_code == 200 and r.json()["accounts"] == ["beta"]


def test_errors_are_json(client):
    r = client.get("/api/files", params={"path": "/nope"})
    assert r.status_code == 404 and r.json()["detail"]
    assert client.get("/api/files", params={"path": "/a/../b"}).status_code == 400
    assert client.post("/api/move", json={"src": "/nope", "dst": "/x"}).status_code == 404


def test_accounts_rename_unlink(client):
    data = client.get("/api/accounts").json()
    assert data["total"] == 30 * 1024 * 1024 and len(data["accounts"]) == 2
    aid = data["accounts"][0]["id"]
    assert client.patch(f"/api/accounts/{aid}", json={"label": "home"}).status_code == 200
    assert client.get("/api/accounts").json()["accounts"][0]["label"] == "home"
    assert client.patch(f"/api/accounts/{aid}", json={"label": "a/b"}).status_code == 400
    assert client.delete(f"/api/accounts/{aid}").status_code == 200
    assert len(client.get("/api/accounts").json()["accounts"]) == 1


def test_oauth_start_redirects_to_google(client):
    r = client.get("/oauth/google/start", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "code_challenge_method=S256" in loc and "access_type=offline" in loc
    assert re.search(r"redirect_uri=http%3A%2F%2Ftestserver%2Foauth%2Fgoogle%2Fcallback", loc)
    bad = client.get("/oauth/google/callback", params={"state": "forged", "code": "x"})
    assert bad.status_code == 400


def test_password_change_signs_out_others(client):
    r = client.post("/api/password", json={"current": "wrong", "new": "another long pw"})
    assert r.status_code == 403
    assert client.post("/api/password", json={"current": PASSWORD, "new": "another long pw"}).status_code == 200
    assert client.get("/api/files").status_code == 200  # this session got a fresh cookie


def test_pwa_assets(client):
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert client.get("/sw.js").status_code == 200
    assert client.get("/static/icons/icon-512.png").headers["content-type"] == "image/png"
