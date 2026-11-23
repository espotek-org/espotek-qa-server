"""Guest platforms (one golden VM image each) and test targets (one release
asset each, installed into a fresh overlay of its platform's image)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from contextlib import AbstractContextManager
from typing import Callable, Protocol

from ..checks import Checks
from ..config import Config
from ..guest import Guest
from ..session import Session


@dataclass(frozen=True)
class Installed:
    labrador: str  # path inside the guest
    labrador_cli: str


class Platform(Protocol):
    name: str
    has_image: bool  # True: a golden VM image; False: runs natively on the host
    ssh_port: int
    boot_timeout_s: float

    def build_base(self, cfg: Config, *, force: bool) -> None: ...

    def session(self, cfg: Config, work: Path, logs: Path) -> AbstractContextManager[Session]:
        """A fresh environment for one target (a throwaway VM, or a reset host account)."""
        ...

    def guest(self) -> Guest: ...

    def stage_dir(self) -> str:
        """Guest-side directory for a run, relative to the qa user's home (for scp)."""
        ...

    def abs_path(self, *parts: str) -> str:
        """Absolute guest path of stage_dir()/parts (for commands run in the guest)."""
        ...

    def mkdir_stage_cmd(self) -> str: ...

    def file_exists_cmd(self, path: str) -> str:
        """Shell command that exits 0 iff the guest file exists."""
        ...

    def start_auto_qa(self, guest: Guest, argv: list[str]) -> None:
        """Start `labrador --auto-qa <dir>` detached, in the desktop session, with stdout/stderr
        going to <stage>/auto-qa.log and the exit code to <stage>/exit_code when it ends."""
        ...

    def exit_code_or_none(self, guest: Guest) -> int | None:
        """--auto-qa's exit code once it has ended; None while it is still running."""
        ...

    def last_line(self, guest: Guest, path: str) -> str:
        """Last line of a guest file, or "" if it doesn't exist (yet) or is empty."""
        ...

    def stop_auto_qa(self, guest: Guest) -> None: ...


class Target(Protocol):
    id: str
    platform: Platform
    asset_name: str

    def install(self, guest: Guest, asset: Path, checks: Checks, log_to: Path,
                snap: Callable[[str], None]) -> Installed:
        """Install the asset as a user would, recording checks. `snap(caption)` takes a VM screenshot."""
        ...
