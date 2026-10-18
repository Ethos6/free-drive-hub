"""Demo server with a fixed WebDAV app password, used by CI to run litmus."""
import sys
import tempfile
from pathlib import Path

import uvicorn

from drivehub.app import create_app
from drivehub.config import Settings
from drivehub.demo import demo_backends
from drivehub.security import Vault, hash_password, token_hash
from drivehub.store import Store

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
tmp = Path(tempfile.mkdtemp())
settings = Settings(tmp, f"http://127.0.0.1:{port}", "", "", "ci-secret", demo=True)
store = Store(tmp / "drivehub.db")
vault = Vault(settings.secret_key)
backends = demo_backends()
for label, backend in backends.items():
    store.upsert_account(backend.email, label, vault.seal(label))
store.set("password_hash", hash_password("demo-password"))
store.add_app_password("litmus", token_hash("litmus-app-password"))
uvicorn.run(create_app(settings, store, lambda row, token: backends[token]),
            host="127.0.0.1", port=port, log_level="warning")
