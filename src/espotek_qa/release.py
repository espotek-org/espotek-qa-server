"""The release under test, from GitHub."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import PATHS
from .download import download


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    sha256: str
    updated_at: str
    url: str


@dataclass(frozen=True)
class Release:
    repo: str
    tag: str
    name: str
    commit: str
    published_at: str
    assets: dict[str, Asset]

    @property
    def fingerprint(self) -> str:
        """Changes whenever the commit or any asset changes (the tag is rolling)."""
        parts = [self.commit] + sorted(f"{a.name}={a.sha256}" for a in self.assets.values())
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]

    def to_json(self) -> dict:
        return {
            "repo": self.repo, "tag": self.tag, "name": self.name, "commit": self.commit,
            "published_at": self.published_at, "fingerprint": self.fingerprint,
            "assets": {n: {"size": a.size, "sha256": a.sha256, "updated_at": a.updated_at}
                       for n, a in self.assets.items()},
        }


def fetch_release(repo: str, tag: str) -> Release:
    r = httpx.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
                  headers={"Accept": "application/vnd.github+json"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    commit = d["target_commitish"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"{repo}@{tag}: target_commitish {commit!r} is not a commit sha")
    assets = {}
    for a in d["assets"]:
        algo, digest = a["digest"].split(":", 1)
        if algo != "sha256":
            raise ValueError(f"{a['name']}: unexpected digest algorithm {algo}")
        assets[a["name"]] = Asset(a["name"], a["size"], digest, a["updated_at"], a["browser_download_url"])
    return Release(repo, tag, d["name"], commit, d["published_at"], assets)


def fetch_asset(release: Release, name: str) -> Path:
    asset = release.assets[name]
    dest = PATHS.cache / "releases" / release.commit / name
    return download(asset.url, dest, sha256=asset.sha256)
