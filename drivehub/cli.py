"""Command line: ``drivehub serve``, ``drivehub demo``, ``drivehub set-password``."""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
import tempfile
from pathlib import Path

from . import __version__
from .config import Settings
from .security import Vault, hash_password
from .store import Store


def _serve(app, host: str, port: int) -> None:
    import uvicorn
    # Proxy headers are handled by the app itself (DRIVEHUB_TRUST_PROXY), so a client
    # cannot fake its address to dodge login rate limits.
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level="info")


def cmd_serve(args: argparse.Namespace) -> None:
    from .app import create_app
    settings = Settings.from_env()
    if not settings.google_configured:
        print("Note: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set, so you cannot link "
              "accounts yet. See README > Google setup.", file=sys.stderr)
    _serve(create_app(settings), args.host, args.port)


def cmd_demo(args: argparse.Namespace) -> None:
    from .app import create_app
    from .demo import demo_backends
    tmp = Path(tempfile.mkdtemp(prefix="drivehub-demo-"))
    settings = Settings(data_dir=tmp, base_url=f"http://localhost:{args.port}",
                        google_client_id="", google_client_secret="",
                        secret_key="demo-only-secret", demo=True)
    store = Store(tmp / "drivehub.db")
    vault = Vault(settings.secret_key)
    backends = demo_backends()
    for label, b in backends.items():
        store.upsert_account(b.email, label, vault.seal(label))
    store.set("password_hash", hash_password("demo-password"))
    print("\n  Demo running with 3 sample accounts (in memory, nothing is saved).")
    print(f"  Open http://localhost:{args.port} and log in with password: demo-password\n")
    _serve(create_app(settings, store, lambda row, token: backends[token]), args.host, args.port)


def cmd_set_password(args: argparse.Namespace) -> None:
    settings = Settings.from_env()
    store = Store(settings.data_dir / "drivehub.db")
    pw = getpass.getpass("New password (10+ characters): ")
    if len(pw) < 10 or pw != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match or are too short.")
    store.set("password_hash", hash_password(pw))
    store.delete_all_sessions()
    if args.reset_2fa:
        store.set("totp_secret", None)
    print("Password updated. All sessions were signed out.")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="drivehub", description="Many Google Drives, one drive.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn, port in (("serve", cmd_serve, 8080), ("demo", cmd_demo, 8080)):
        s = sub.add_parser(name)
        s.add_argument("--host", default="0.0.0.0" if name == "serve" else "127.0.0.1")
        s.add_argument("--port", type=int, default=port)
        s.set_defaults(fn=fn)
    s = sub.add_parser("set-password", help="set or reset the login password")
    s.add_argument("--reset-2fa", action="store_true", help="also turn off two-factor login")
    s.set_defaults(fn=cmd_set_password)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
