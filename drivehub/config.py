"""Settings, read from environment variables (or a .env file next to the app)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    data_dir: Path
    base_url: str
    google_client_id: str
    google_client_secret: str
    secret_key: str
    demo: bool = False
    session_days: int = 30
    trust_proxy: bool = False

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/oauth/google/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @classmethod
    def from_env(cls, demo: bool = False) -> "Settings":
        _load_dotenv(Path.cwd() / ".env")
        data_dir = Path(os.environ.get("DRIVEHUB_DATA", "./data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        secret = os.environ.get("DRIVEHUB_SECRET_KEY", "")
        if not secret:
            key_file = data_dir / "secret.key"
            if not key_file.exists():
                key_file.write_text(secrets.token_urlsafe(48))
                key_file.chmod(0o600)
            secret = key_file.read_text().strip()
        return cls(
            data_dir=data_dir,
            base_url=os.environ.get("DRIVEHUB_BASE_URL", "http://localhost:8080").rstrip("/"),
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            secret_key=secret,
            demo=demo or os.environ.get("DRIVEHUB_DEMO", "") == "1",
            trust_proxy=os.environ.get("DRIVEHUB_TRUST_PROXY", "") == "1",
        )
