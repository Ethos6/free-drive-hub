"""Sample accounts for ``drivehub demo``: try the app without any Google setup."""
from __future__ import annotations

import struct
import zlib

from .backends.memory import MemoryBackend

GB = 1024**3


def _png(w: int, h: int, rgb: tuple[int, int, int]) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def demo_backends() -> dict[str, MemoryBackend]:
    personal = MemoryBackend("me.personal@gmail.com", 15 * GB)
    work = MemoryBackend("me.work@gmail.com", 15 * GB)
    backup = MemoryBackend("me.backup@gmail.com", 100 * GB)

    r = personal._root
    photos = personal.add_folder(r, "Photos")
    trip = personal.add_folder(photos, "2026 Goa trip")
    personal.add_file(trip, "beach.png", _png(64, 40, (40, 140, 200)), "image/png")
    personal.add_file(trip, "sunset.png", _png(64, 40, (230, 120, 40)), "image/png")
    docs = personal.add_folder(r, "Documents")
    personal.add_file(docs, "notes.txt", b"Shopping list\n- rice\n- tea\n", "text/plain")
    personal.add_file(docs, "Budget", b"budget", "application/vnd.google-apps.spreadsheet")

    r = work._root
    docs_w = work.add_folder(r, "Documents")
    work.add_file(docs_w, "notes.txt", b"Meeting notes\n", "text/plain")
    work.add_file(docs_w, "Project plan", b"plan", "application/vnd.google-apps.document")
    proj = work.add_folder(r, "Projects")
    work.add_file(proj, "website-backup.zip", b"PK" + b"\x00" * 4096, "application/zip")

    r = backup._root
    photos_b = backup.add_folder(r, "Photos")
    old = backup.add_folder(photos_b, "2025")
    backup.add_file(old, "family.png", _png(64, 40, (90, 170, 90)), "image/png")
    backup.add_file(r, "big-video.mp4", b"\x00" * (3 * 1024 * 1024), "video/mp4")

    # make the quota bars interesting
    _fake_usage(personal, int(9.6 * GB))
    _fake_usage(work, int(13.1 * GB))
    _fake_usage(backup, int(21 * GB))
    return {"personal": personal, "work": work, "backup": backup}


def _fake_usage(b: MemoryBackend, used: int) -> None:
    """Report realistic usage without allocating it."""
    real_used = b._used

    def used_fn() -> int:
        return real_used() + used
    b._used = used_fn  # type: ignore[method-assign]


__all__ = ["demo_backends"]
