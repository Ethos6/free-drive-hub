import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from drivehub.app import create_app
from drivehub.backends.memory import MemoryBackend
from drivehub.config import Settings
from drivehub.hub import Account, Hub
from drivehub.security import Vault, hash_password
from drivehub.store import Store

MB = 1024 * 1024
PASSWORD = "correct horse battery"


@pytest.fixture
def backends():
    a = MemoryBackend("a@example.com", total=10 * MB)
    b = MemoryBackend("b@example.com", total=20 * MB)
    return a, b


@pytest.fixture
def hub(backends):
    a, b = backends
    return Hub([Account(1, "alpha", a.email, a), Account(2, "beta", b.email, b)],
               list_ttl=0, quota_ttl=0)


def blob(n: int) -> io.BytesIO:
    return io.BytesIO(b"x" * n)


@pytest.fixture
def make_client(tmp_path: Path, backends):
    def make(password: str | None = PASSWORD, accounts: bool = True):
        settings = Settings(data_dir=tmp_path, base_url="http://testserver",
                            google_client_id="cid", google_client_secret="csecret",
                            secret_key="test-secret")
        store = Store(tmp_path / "t.db")
        if password:
            store.set("password_hash", hash_password(password))
        by_token = {}
        if accounts:
            vault = Vault(settings.secret_key)
            for label, b in zip(("alpha", "beta"), backends):
                store.upsert_account(b.email, label, vault.seal(label))
                by_token[label] = b
        app = create_app(settings, store, lambda row, token: by_token[token])
        app.state.hub.list_ttl = 0
        app.state.hub.quota_ttl = 0
        return TestClient(app)
    return make


@pytest.fixture
def client(make_client):
    with make_client() as c:
        r = c.post("/api/login", json={"password": PASSWORD}, headers={"X-DriveHub": "1"})
        assert r.status_code == 200, r.text
        c.headers["X-DriveHub"] = "1"
        yield c
