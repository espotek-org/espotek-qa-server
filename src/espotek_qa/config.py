"""Host configuration and the on-disk layout of a QA tester.

Everything lives under QA_HOME (/Users/Shared/espotek-qa by default; the
ESPOTEK_QA_HOME override exists for developing this repo on a machine that is
not the tester). bootstrap.sh writes config.toml; every key in it is required.
"""

from __future__ import annotations

import os
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

QA_HOME = Path(os.environ.get("ESPOTEK_QA_HOME", "/Users/Shared/espotek-qa"))

_T0 = time.monotonic()


def log(msg: str) -> None:
    """Progress line with elapsed time since the process started."""
    elapsed = time.monotonic() - _T0
    print(f"[{elapsed:7.1f}s] {msg}", file=sys.stderr, flush=True)


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    @property
    def ssh_key(self) -> Path:
        return self.home / "ssh" / "id_ed25519"

    @property
    def vms(self) -> Path:
        return self.home / "vms"

    def vm_dir(self, platform: str) -> Path:
        return self.vms / platform

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def results(self) -> Path:
        return self.home / "results"

    @property
    def state(self) -> Path:
        return self.home / "state"

    @property
    def run_lock(self) -> Path:
        return self.state / "run.lock"


PATHS = Paths(QA_HOME)


@dataclass(frozen=True)
class Config:
    release_repo: str
    release_tag: str
    board_lock: Path
    usb_ids: list[tuple[int, int]]
    platforms: list[str]
    vm_memory_mb: int
    vm_cpus: int
    qemu_prefix: Path
    windows_password: str


def _parse_usb_id(text: str) -> tuple[int, int]:
    vid, pid = text.split(":")
    return int(vid, 16), int(pid, 16)


def load_config() -> Config:
    with PATHS.config.open("rb") as f:
        raw = tomllib.load(f)
    return Config(
        release_repo=raw["release"]["repo"],
        release_tag=raw["release"]["tag"],
        board_lock=Path(raw["board"]["lock"]),
        usb_ids=[_parse_usb_id(u) for u in raw["board"]["usb_ids"]],
        platforms=list(raw["run"]["platforms"]),
        vm_memory_mb=int(raw["vm"]["memory_mb"]),
        vm_cpus=int(raw["vm"]["cpus"]),
        qemu_prefix=Path(raw["vm"]["qemu_prefix"]),
        windows_password=raw["vm"]["windows_password"],
    )
