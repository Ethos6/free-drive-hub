"""SQLite storage for settings, linked accounts, sessions and app passwords."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'google',
    email TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    token TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS dav_props (
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (path, name)
);
CREATE TABLE IF NOT EXISTS app_passwords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    last_used REAL
);
"""


class Store:
    def __init__(self, path: Path | str):
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def close(self) -> None:
        self._db.close()

    # settings
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._exec("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self._exec("DELETE FROM settings WHERE key=?", (key,))
        else:
            self._exec("INSERT INTO settings(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # accounts
    def accounts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._exec("SELECT * FROM accounts ORDER BY id")]

    def upsert_account(self, email: str, label: str, token: str) -> int:
        row = self._exec("SELECT id FROM accounts WHERE email=?", (email,)).fetchone()
        if row:
            self._exec("UPDATE accounts SET token=? WHERE id=?", (token, row["id"]))
            return row["id"]
        return self._exec("INSERT INTO accounts(email,label,token,created) VALUES(?,?,?,?)",
                          (email, label, token, time.time())).lastrowid

    def rename_account(self, account_id: int, label: str) -> bool:
        return self._exec("UPDATE accounts SET label=? WHERE id=?", (label, account_id)).rowcount > 0

    def delete_account(self, account_id: int) -> bool:
        return self._exec("DELETE FROM accounts WHERE id=?", (account_id,)).rowcount > 0

    # sessions
    def create_session(self, sid_hash: str, days: int, user_agent: str = "") -> None:
        now = time.time()
        self._exec("INSERT INTO sessions(id,created,expires,user_agent) VALUES(?,?,?,?)",
                   (sid_hash, now, now + days * 86400, user_agent[:200]))

    def session_valid(self, sid_hash: str) -> bool:
        row = self._exec("SELECT expires FROM sessions WHERE id=?", (sid_hash,)).fetchone()
        return bool(row and row["expires"] > time.time())

    def delete_session(self, sid_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE id=?", (sid_hash,))

    def delete_all_sessions(self) -> None:
        self._exec("DELETE FROM sessions")

    def purge_expired_sessions(self) -> None:
        self._exec("DELETE FROM sessions WHERE expires < ?", (time.time(),))

    # app passwords (for WebDAV)
    def add_app_password(self, name: str, hashed: str) -> int:
        return self._exec("INSERT INTO app_passwords(name,hash,created) VALUES(?,?,?)",
                          (name, hashed, time.time())).lastrowid

    def app_passwords(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._exec(
            "SELECT id,name,created,last_used FROM app_passwords ORDER BY id")]

    def app_password_by_hash(self, hashed: str) -> Optional[int]:
        row = self._exec("SELECT id FROM app_passwords WHERE hash=?", (hashed,)).fetchone()
        if row:
            self._exec("UPDATE app_passwords SET last_used=? WHERE id=?", (time.time(), row["id"]))
            return row["id"]
        return None

    def delete_app_password(self, pid: int) -> bool:
        return self._exec("DELETE FROM app_passwords WHERE id=?", (pid,)).rowcount > 0

    # WebDAV dead properties (e.g. Windows file times), keyed by unified path
    def props(self, path: str) -> dict[str, str]:
        return {r["name"]: r["value"] for r in self._exec(
            "SELECT name, value FROM dav_props WHERE path=?", (path,))}

    def set_props(self, path: str, updates: dict[str, Optional[str]]) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for name, value in updates.items():
                    if value is None:
                        self._db.execute("DELETE FROM dav_props WHERE path=? AND name=?", (path, name))
                    else:
                        self._db.execute("INSERT INTO dav_props(path,name,value) VALUES(?,?,?) "
                                         "ON CONFLICT(path,name) DO UPDATE SET value=excluded.value",
                                         (path, name, value))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    @staticmethod
    def _like(path: str) -> str:
        return path.rstrip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"

    def delete_props(self, path: str) -> None:
        self._exec("DELETE FROM dav_props WHERE path=? OR path LIKE ? ESCAPE '\\'",
                   (path, self._like(path)))

    def copy_props(self, src: str, dst: str, move: bool = False) -> None:
        rows = self._exec("SELECT path, name, value FROM dav_props WHERE path=? OR path LIKE ? ESCAPE '\\'",
                          (src, self._like(src))).fetchall()
        self.delete_props(dst)
        for r in rows:
            new = dst + r["path"][len(src):]
            self._exec("INSERT OR REPLACE INTO dav_props(path,name,value) VALUES(?,?,?)",
                       (new, r["name"], r["value"]))
        if move:
            self.delete_props(src)
