"""HTTP application: web UI, JSON API, Google OAuth linking and WebDAV."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import segno
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .backends.base import BackendError
from .backends.google import GoogleDriveBackend, authorization_url, exchange_code, pkce_pair
from .config import Settings
from .hub import Account, Hub, HubError, join, split
from .security import (Throttle, Vault, hash_password, new_totp_secret, token_hash,
                       totp_uri, verify_password, verify_totp)
from .store import Store
from .webdav import WebDAV, basic_credentials

log = logging.getLogger("drivehub")
STATIC = Path(__file__).parent / "static"
COOKIE = "dh_session"
SAFE_INLINE = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
               "application/pdf", "text/plain", "audio/", "video/")


def create_app(settings: Settings, store: Optional[Store] = None,
               backend_factory=None) -> FastAPI:
    """``backend_factory(account_row, refresh_token)`` builds a backend; tests and the
    demo pass their own, production uses Google Drive."""
    store = store or Store(settings.data_dir / "drivehub.db")
    vault = Vault(settings.secret_key)
    hub = Hub()
    throttle = Throttle()
    oauth_pending: dict[str, tuple[str, float]] = {}
    state: dict[str, Any] = {"setup_token": None}

    def default_factory(row: dict, refresh_token: str):
        return GoogleDriveBackend(settings.google_client_id, settings.google_client_secret,
                                  refresh_token)

    factory = backend_factory or default_factory

    async def load_accounts() -> None:
        old = hub.accounts
        accounts = []
        for row in store.accounts():
            try:
                backend = factory(row, vault.open(row["token"]))
            except ValueError as e:
                log.error("account %s: %s", row["email"], e)
                continue
            accounts.append(Account(row["id"], row["label"], row["email"], backend))
        hub.set_accounts(accounts)
        for acc in old:
            await acc.backend.aclose()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.purge_expired_sessions()
        if not store.get("password_hash"):
            state["setup_token"] = secrets.token_urlsafe(12)
            log.warning("First run: open %s and use setup code: %s", settings.base_url,
                        state["setup_token"])
            print(f"\n  Drive Hub setup code: {state['setup_token']}\n", flush=True)
        await load_accounts()
        yield
        for acc in hub.accounts:
            await acc.backend.aclose()

    app = FastAPI(title="Drive Hub", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub, app.state.store, app.state.settings = hub, store, settings
    app.state.reload_accounts = load_accounts
    app.state.setup = state

    # -- helpers ------------------------------------------------------------------
    def client_ip(request: Request) -> str:
        if settings.trust_proxy:
            # the right-most entry was added by our own proxy; earlier ones are client-supplied
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                return fwd.split(",")[-1].strip()
        return request.client.host if request.client else "?"

    def logged_in(request: Request) -> bool:
        sid = request.cookies.get(COOKIE)
        return bool(sid) and store.session_valid(token_hash(sid))

    def require_login(request: Request) -> None:
        if not logged_in(request):
            raise HTTPException(401, "login required")

    def start_session(response: Response, request: Request) -> None:
        sid = secrets.token_urlsafe(32)
        store.create_session(token_hash(sid), settings.session_days,
                             request.headers.get("user-agent", ""))
        response.set_cookie(COOKIE, sid, max_age=settings.session_days * 86400, httponly=True,
                            samesite="lax", secure=settings.secure_cookies, path="/")

    def dav_auth(request: Request) -> bool:
        # Basic auth with an app password only. Browser session cookies are NOT accepted
        # here, so a file opened under /dav can never act as the signed-in user.
        creds = basic_credentials(request)
        if not creds:
            return False
        ip = client_ip(request)
        if throttle.blocked("dav:" + ip):
            return False
        if store.app_password_by_hash(token_hash(creds[1])) is not None:
            return True
        throttle.fail("dav:" + ip)
        return False

    def entry_list(entries) -> list[dict]:
        return [e.to_dict() for e in entries]

    # -- middleware -----------------------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if (path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get("x-drivehub") != "1"):
            return JSONResponse({"detail": "missing X-DriveHub header"}, 403)
        try:
            response = await call_next(request)
        except HubError as e:
            response = JSONResponse({"detail": str(e)}, e.status)
        except BackendError as e:
            response = JSONResponse({"detail": str(e)}, e.status)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; frame-src 'self'; object-src 'none'; "
            "frame-ancestors 'self'; base-uri 'none'; form-action 'self'")
        return response

    @app.exception_handler(HubError)
    async def hub_error(request: Request, exc: HubError):
        return JSONResponse({"detail": str(exc)}, exc.status)

    @app.exception_handler(BackendError)
    async def backend_error(request: Request, exc: BackendError):
        return JSONResponse({"detail": str(exc)}, exc.status)

    # -- pages ----------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest",
                            media_type="application/manifest+json")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": __version__}

    # -- auth -----------------------------------------------------------------------
    @app.get("/api/status")
    async def status(request: Request):
        return {
            "version": __version__,
            "setup_needed": not store.get("password_hash"),
            "logged_in": logged_in(request),
            "totp_enabled": bool(store.get("totp_secret")),
            "google_configured": settings.google_configured,
            "demo": settings.demo,
            "dav_url": settings.base_url + "/dav/",
        }

    @app.post("/api/setup")
    async def setup(request: Request, body: dict = Body(...)):
        if store.get("password_hash"):
            raise HTTPException(409, "already set up")
        ip = client_ip(request)
        if throttle.blocked(ip):
            raise HTTPException(429, "too many attempts; wait 15 minutes")
        token = state["setup_token"]
        if not token or not secrets.compare_digest(str(body.get("setup_code", "")), token):
            throttle.fail(ip)
            raise HTTPException(403, "wrong setup code (it is printed in the server log)")
        password = str(body.get("password", ""))
        if len(password) < 10:
            raise HTTPException(400, "use at least 10 characters")
        store.set("password_hash", hash_password(password))
        state["setup_token"] = None
        resp = JSONResponse({"ok": True})
        start_session(resp, request)
        return resp

    @app.post("/api/login")
    async def login(request: Request, body: dict = Body(...)):
        ip = client_ip(request)
        if throttle.blocked(ip):
            raise HTTPException(429, "too many attempts; wait 15 minutes")
        stored = store.get("password_hash")
        ok = bool(stored) and await asyncio.to_thread(
            verify_password, str(body.get("password", "")), stored)
        secret = store.get("totp_secret")
        if ok and secret and not verify_totp(secret, str(body.get("code", ""))):
            throttle.fail(ip)
            raise HTTPException(401, "wrong or missing 2FA code" if body.get("code") else
                                "2FA code required")
        if not ok:
            throttle.fail(ip)
            raise HTTPException(401, "wrong password")
        throttle.reset(ip)
        resp = JSONResponse({"ok": True})
        start_session(resp, request)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        sid = request.cookies.get(COOKIE)
        if sid:
            store.delete_session(token_hash(sid))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/password")
    async def change_password(request: Request, body: dict = Body(...)):
        require_login(request)
        if not await asyncio.to_thread(verify_password, str(body.get("current", "")),
                                       store.get("password_hash") or ""):
            raise HTTPException(403, "current password is wrong")
        new = str(body.get("new", ""))
        if len(new) < 10:
            raise HTTPException(400, "use at least 10 characters")
        store.set("password_hash", hash_password(new))
        store.delete_all_sessions()
        resp = JSONResponse({"ok": True})
        start_session(resp, request)
        return resp

    @app.post("/api/totp/setup")
    async def totp_setup(request: Request):
        require_login(request)
        secret = new_totp_secret()
        uri = totp_uri(secret, "drivehub")
        svg = segno.make(uri, error="m").svg_inline(scale=5, dark="#111", light="#fff")
        return {"secret": secret, "uri": uri, "qr_svg": svg}

    @app.post("/api/totp/enable")
    async def totp_enable(request: Request, body: dict = Body(...)):
        require_login(request)
        secret, code = str(body.get("secret", "")), str(body.get("code", ""))
        if not secret or not verify_totp(secret, code):
            raise HTTPException(400, "code does not match; check your phone's clock")
        store.set("totp_secret", secret)
        return {"ok": True}

    @app.post("/api/totp/disable")
    async def totp_disable(request: Request, body: dict = Body(...)):
        require_login(request)
        if not await asyncio.to_thread(verify_password, str(body.get("password", "")),
                                       store.get("password_hash") or ""):
            raise HTTPException(403, "password is wrong")
        store.set("totp_secret", None)
        return {"ok": True}

    @app.get("/api/app-passwords")
    async def list_app_passwords(request: Request):
        require_login(request)
        return store.app_passwords()

    @app.post("/api/app-passwords")
    async def create_app_password(request: Request, body: dict = Body(...)):
        require_login(request)
        name = str(body.get("name", "")).strip()[:60] or "WebDAV"
        raw = "-".join(secrets.token_hex(4) for _ in range(4))
        pid = store.add_app_password(name, token_hash(raw))
        return {"id": pid, "name": name, "password": raw}

    @app.delete("/api/app-passwords/{pid}")
    async def delete_app_password(pid: int, request: Request):
        require_login(request)
        if not store.delete_app_password(pid):
            raise HTTPException(404, "not found")
        return {"ok": True}

    # -- accounts -------------------------------------------------------------------
    @app.get("/api/accounts")
    async def accounts(request: Request):
        require_login(request)
        rows = []
        total = used = 0
        unlimited = False
        for acc, q in await hub.quotas():
            if q:
                used += q.used
                if q.total is None:
                    unlimited = True
                else:
                    total += q.total
            rows.append({"id": acc.id, "label": acc.label, "email": acc.email,
                         "total": q.total if q else None, "used": q.used if q else None,
                         "error": hub.errors.get(acc.id)})
        return {"accounts": rows, "total": None if unlimited else total, "used": used}

    @app.get("/oauth/google/start", include_in_schema=False)
    async def oauth_start(request: Request):
        require_login(request)
        if not settings.google_configured:
            raise HTTPException(400, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first")
        now = time.time()
        for k in [k for k, (_, t) in oauth_pending.items() if now - t > 600]:
            oauth_pending.pop(k, None)
        verifier, challenge = pkce_pair()
        st = secrets.token_urlsafe(24)
        oauth_pending[st] = (verifier, now)
        return RedirectResponse(authorization_url(settings.google_client_id, settings.redirect_uri,
                                                  st, challenge), 303)

    @app.get("/oauth/google/callback", include_in_schema=False)
    async def oauth_callback(request: Request, state_: str = Query("", alias="state"),
                             code: str = "", error: str = ""):
        require_login(request)
        pending = oauth_pending.pop(state_, None)
        if error:
            return RedirectResponse("/#/accounts?error=" + quote(error), 303)
        if not pending or not code:
            raise HTTPException(400, "invalid or expired sign-in attempt; try again")
        tokens = await exchange_code(settings.google_client_id, settings.google_client_secret,
                                     settings.redirect_uri, code, pending[0])
        probe = GoogleDriveBackend(settings.google_client_id, settings.google_client_secret,
                                   tokens["refresh_token"])
        try:
            email = (await probe.quota()).email
        finally:
            await probe.aclose()
        label = email.split("@")[0][:30]
        store.upsert_account(email, label, vault.seal(tokens["refresh_token"]))
        await load_accounts()
        return RedirectResponse("/#/accounts?linked=" + quote(email), 303)

    @app.patch("/api/accounts/{account_id}")
    async def rename_account(account_id: int, request: Request, body: dict = Body(...)):
        require_login(request)
        label = str(body.get("label", "")).strip()[:30]
        if not label or "/" in label:
            raise HTTPException(400, "invalid label")
        if not store.rename_account(account_id, label):
            raise HTTPException(404, "not found")
        await load_accounts()
        return {"ok": True}

    @app.delete("/api/accounts/{account_id}")
    async def unlink_account(account_id: int, request: Request):
        require_login(request)
        if not store.delete_account(account_id):
            raise HTTPException(404, "not found")
        await load_accounts()
        return {"ok": True}

    # -- files ----------------------------------------------------------------------
    @app.get("/api/files")
    async def list_files(request: Request, path: str = "/"):
        require_login(request)
        entries = await hub.ls(path)
        return {"path": join(split(path)), "entries": entry_list(entries),
                "errors": [{"account": a.label, "error": hub.errors[a.id]}
                           for a in hub.accounts if a.id in hub.errors]}

    @app.get("/api/search")
    async def search(request: Request, q: str = ""):
        require_login(request)
        return {"results": await hub.search(q)}

    @app.get("/api/download")
    async def download(request: Request, path: str, inline: bool = False):
        require_login(request)
        entry, dl = await hub.read(path, request.headers.get("range"))
        safe = inline and any(entry.mime.startswith(t) for t in SAFE_INLINE)
        disp = "inline" if safe else "attachment"
        pdf = safe and entry.mime == "application/pdf"
        csp = ("default-src 'none'; frame-ancestors 'self'" if pdf else
               "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; "
               "style-src 'unsafe-inline'; frame-ancestors 'self'")
        headers = {"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(entry.name)}",
                   "Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
                   "Content-Security-Policy": csp, "X-Frame-Options": "SAMEORIGIN"}
        if dl.size is not None:
            headers["Content-Length"] = str(dl.size)
        if dl.content_range:
            headers["Content-Range"] = dl.content_range
        mime = dl.mime if safe else (dl.mime or "application/octet-stream")
        if mime.startswith("text/"):
            mime = "text/plain; charset=utf-8"

        async def body():
            try:
                async for chunk in dl.chunks:
                    yield chunk
            finally:
                await dl.aclose()
        return StreamingResponse(body(), dl.status, headers=headers, media_type=mime)

    @app.post("/api/upload")
    async def upload(request: Request, path: str = Form("/"),
                     files: list[UploadFile] = File(...),
                     relpaths: Optional[list[str]] = Form(None)):
        require_login(request)
        base = split(path)
        done = []
        for i, f in enumerate(files):
            rel = (relpaths[i] if relpaths and i < len(relpaths) and relpaths[i] else f.filename)
            parts = split(rel or "upload")
            for depth in range(1, len(parts)):  # create folders of a folder upload
                sub = join(base + parts[:depth])
                if not await hub.exists(sub):
                    await hub.mkdir(sub)
            size = f.size if f.size is not None else _size_of(f.file)
            entry = await hub.write(join(base + parts), f.file, size, f.content_type)
            done.append(entry.to_dict())
        return {"uploaded": done}

    @app.post("/api/mkdir")
    async def mkdir(request: Request, body: dict = Body(...)):
        require_login(request)
        return (await hub.mkdir(str(body.get("path", "")))).to_dict()

    @app.post("/api/move")
    async def move(request: Request, body: dict = Body(...)):
        require_login(request)
        e = await hub.move(str(body["src"]), str(body["dst"]), bool(body.get("overwrite")))
        return e.to_dict()

    @app.post("/api/copy")
    async def copy(request: Request, body: dict = Body(...)):
        require_login(request)
        e = await hub.copy(str(body["src"]), str(body["dst"]), bool(body.get("overwrite")))
        return e.to_dict()

    @app.post("/api/transfer")
    async def transfer(request: Request, body: dict = Body(...)):
        require_login(request)
        return (await hub.transfer(str(body["path"]), int(body["account_id"]))).to_dict()

    @app.post("/api/delete")
    async def delete(request: Request, body: dict = Body(...)):
        require_login(request)
        for p in body.get("paths", []):
            await hub.delete(str(p))
        return {"ok": True}

    @app.post("/api/share")
    async def share(request: Request, body: dict = Body(...)):
        require_login(request)
        return {"links": await hub.share(str(body.get("path", "")))}

    @app.post("/api/refresh")
    async def refresh(request: Request):
        require_login(request)
        hub.invalidate()
        return {"ok": True}

    # -- WebDAV -----------------------------------------------------------------------
    dav = WebDAV(hub, "/dav", dav_auth, props=store)
    methods = ["GET", "HEAD", "PUT", "DELETE", "OPTIONS", "PROPFIND", "PROPPATCH", "MKCOL",
               "COPY", "MOVE", "LOCK", "UNLOCK"]
    app.add_api_route("/dav", dav, methods=methods, include_in_schema=False)
    app.add_api_route("/dav/{rest:path}", dav, methods=methods, include_in_schema=False)
    return app


def _size_of(f) -> int:
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    return size
