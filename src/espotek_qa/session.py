"""Where a target runs: a throwaway VM (Linux, Windows) or a dedicated account
on the host (macOS). Either way the runner gets a Guest to ssh into."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import PATHS, Config
from .guest import Guest
from .qemu import Vm, create_overlay


@dataclass
class Session:
    guest: Guest
    alive: Callable[[], bool]
    # Host-side capture of the whole display, or None where the host can't see it.
    screendump: Callable[[Path], None] | None


@contextmanager
def vm_session(cfg: Config, platform, work: Path, logs: Path) -> Iterator[Session]:
    """Boot a fresh qcow2 overlay of the platform's golden image; discard it afterwards."""
    vmdir = PATHS.vm_dir(platform.name)
    base = vmdir / "base.qcow2"
    if not base.exists():
        raise FileNotFoundError(f"no golden image for {platform.name}; run `qa vm build {platform.name}`")
    work.mkdir(parents=True)
    overlay = work / "disk.qcow2"
    efivars = work / "efivars.fd"
    create_overlay(cfg, base, overlay)
    shutil.copyfile(vmdir / "efivars.fd", efivars)
    vm = Vm(cfg, platform.run_spec(cfg, overlay, efivars), work)
    guest = platform.guest()
    try:
        vm.start()
        guest.wait_ssh(platform.boot_timeout_s, alive=vm.running)
        yield Session(guest=guest, alive=vm.running, screendump=vm.screendump)
    finally:
        if vm.running():
            vm.shutdown()
        vm.kill()
        for name in ("serial.log", "qemu.log", "qemu.argv"):
            if (work / name).exists():
                shutil.copyfile(work / name, logs / name)
        shutil.rmtree(work)
