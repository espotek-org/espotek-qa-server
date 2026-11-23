"""Verified downloads with progress output."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

from .config import log


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, *, sha256: str, headers: dict | None = None) -> Path:
    """Download url to dest unless dest already has the expected sha256."""
    if dest.exists() and sha256_file(dest) == sha256:
        log(f"cached {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    t0 = last = time.monotonic()
    done = 0
    with httpx.stream("GET", url, headers=headers or {}, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers["content-length"]) if "content-length" in r.headers else None
        with part.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last > 10:
                    pct = f" ({100 * done / total:.0f}%)" if total else ""
                    log(f"downloading {dest.name}: {done / 1e6:.0f} MB{pct}, {done / 1e6 / (now - t0):.1f} MB/s")
                    last = now
    got = h.hexdigest()
    if got != sha256:
        part.unlink()
        raise ValueError(f"{dest.name}: sha256 {got} does not match expected {sha256}")
    part.rename(dest)
    log(f"downloaded {dest.name} ({done / 1e6:.0f} MB in {time.monotonic() - t0:.0f}s)")
    return dest
