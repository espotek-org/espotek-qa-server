"""macOS target, run natively on the host in a dedicated standard account
(`qamac`), which is the console (auto-login) user so the app has a real
desktop, GPU and USB stack. A macOS VM on an Intel host would cost half the
RAM and still be capped at the host's macOS version, so the account, wiped of
the app and its data before every run, takes the VM's place.

The runner reaches the account over ssh to 127.0.0.1 with the same key it uses
for the VMs."""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..checks import Checks
from ..config import Config, log
from ..guest import Guest
from ..session import Session
from . import Installed

USER = "qamac"
HOME = f"/Users/{USER}"
STAGE = "qa-run"
APP = f"{HOME}/Applications/Labrador.app"

# Everything a previous run (or a user) could have left behind.
RESET = (
    "pkill -x labrador; pkill -x labrador-cli; "
    "for v in /Volumes/Labrador*; do [ -d \"$v\" ] && hdiutil detach -force \"$v\" >/dev/null; done; "
    f"rm -rf {APP} ~/{STAGE} \"$HOME/Library/Application Support/EspoTek\" "
    "\"$HOME/Library/Saved Application State/\"com.espotek.* $HOME/Library/Preferences/com.espotek.* "
    "$HOME/Library/Caches/com.espotek.*; true"
)


class MacPlatform:
    name = "macos"
    has_image = False  # a golden VM image built by `qa vm build`
    ssh_port = 22
    boot_timeout_s = 30.0

    def guest(self) -> Guest:
        return Guest(self.name, self.ssh_port, user=USER)

    def stage_dir(self) -> str:
        return STAGE

    def abs_path(self, *parts: str) -> str:
        return "/".join([f"{HOME}/{STAGE}", *parts])

    def mkdir_stage_cmd(self) -> str:
        return f"rm -rf ~/{STAGE} && mkdir -p ~/{STAGE}"

    def file_exists_cmd(self, path: str) -> str:
        return f"test -f {shlex.quote(path)}"

    def build_base(self, cfg: Config, *, force: bool) -> None:
        raise NotImplementedError("macOS runs natively in the qamac account, which bootstrap.sh sets up; "
                                  "there is no image to build")

    @contextmanager
    def session(self, cfg: Config, work: Path, logs: Path) -> Iterator[Session]:
        g = self.guest()
        g.wait_ssh(self.boot_timeout_s)
        console = g.run("stat -f %Su /dev/console", timeout=30).stdout.strip()
        if console != USER:
            raise RuntimeError(f"macos: the console user is {console!r}, not {USER!r}, so the app would have no "
                               f"desktop; set {USER} as the auto-login user (bootstrap.sh does) and reboot")
        g.run(RESET, timeout=120)
        try:
            yield Session(guest=g, alive=lambda: True, screendump=None)
        finally:
            g.run(RESET, timeout=120, check=False)

    def start_auto_qa(self, guest: Guest, argv: list[str]) -> None:
        cmd = " ".join(shlex.quote(a) for a in argv)
        inner = (f"cd ~/{STAGE} && {cmd} > ~/{STAGE}/auto-qa.log 2>&1; "
                 f"echo $? > ~/{STAGE}/exit_code.tmp && mv ~/{STAGE}/exit_code.tmp ~/{STAGE}/exit_code")
        guest.run(f"nohup sh -c {shlex.quote(inner)} > /dev/null 2>&1 &", timeout=60)

    def exit_code_or_none(self, guest: Guest) -> int | None:
        text = guest.run(f"cat ~/{STAGE}/exit_code 2>/dev/null || true", timeout=60).stdout.strip()
        return int(text) if text else None

    def last_line(self, guest: Guest, path: str) -> str:
        return guest.run(f"tail -n 1 {shlex.quote(path)} 2>/dev/null || true", timeout=60).stdout.strip()

    def stop_auto_qa(self, guest: Guest) -> None:
        guest.run("pkill -x labrador; true", timeout=60, check=False)


class MacTarget:
    id = "macos"
    asset_name = "Labrador-for-macOS.dmg"

    def __init__(self, platform: MacPlatform):
        self.platform = platform

    def install(self, guest: Guest, asset: Path, checks: Checks, log_to: Path, snap) -> Installed:
        r = guest.run("system_profiler SPUSBDataType | grep -A4 -i labrador", timeout=120, check=False)
        checks.require("board_visible", "Board visible on the host's USB bus", "0xba94" in r.stdout.lower()
                       or "0x2fe4" in r.stdout.lower(), r.stdout.strip())
        dmg = f"{HOME}/{STAGE}/{asset.name}"
        guest.put(asset, f"{STAGE}/{asset.name}")
        # Mark it as a browser download so Gatekeeper treats it the way it treats a user's.
        guest.run(f"xattr -w com.apple.quarantine \"0081;$(printf %x $(date +%s));Safari;\" {dmg}", timeout=60)
        # The ticket is stapled to the outermost container, the DMG (Apple's guidance), so a user's
        # Mac can verify it offline; the app copied out of it carries none of its own.
        r = guest.run(f"xcrun stapler validate {dmg}", timeout=300, check=False, log_to=log_to)
        checks.record("stapled", "Notarization ticket is stapled to the DMG", r.returncode == 0,
                      (r.stdout + r.stderr).strip()[-300:])
        mnt = f"{HOME}/{STAGE}/mnt"
        r = guest.run(f"mkdir -p {mnt} && hdiutil attach -nobrowse -readonly -mountpoint {mnt} {dmg}",
                      timeout=300, check=False, log_to=log_to)
        checks.require("dmg_mounts", "DMG mounts", r.returncode == 0, r.stderr.strip()[-300:])
        r = guest.run(f"mkdir -p ~/Applications && ditto {mnt}/Labrador.app {APP} && hdiutil detach {mnt}",
                      timeout=300, check=False, log_to=log_to)
        checks.require("app_copied", "Labrador.app copies out of the DMG into ~/Applications", r.returncode == 0,
                       r.stderr.strip()[-300:])
        r = guest.run(f"codesign --verify --deep --strict --verbose=2 {APP}", timeout=300, check=False, log_to=log_to)
        checks.record("codesign", "Code signature verifies (deep, strict)", r.returncode == 0, r.stderr.strip()[-400:])
        r = guest.run(f"spctl --assess --type execute -vv {APP}", timeout=300, check=False, log_to=log_to)
        checks.record("gatekeeper", "Gatekeeper accepts the downloaded app as notarized", r.returncode == 0
                      and "Notarized Developer ID" in r.stderr, r.stderr.strip()[-400:])
        r = guest.run(f"lipo -archs {APP}/Contents/MacOS/labrador", timeout=60, check=False)
        archs = r.stdout.split()
        checks.record("universal", "labrador is a universal binary (x86_64 and arm64)",
                      "x86_64" in archs and "arm64" in archs, r.stdout.strip())
        for f in ("Contents/MacOS/labrador-cli", "Contents/Resources/assets/firmware/labrafirm_000C_03.hex"):
            r = guest.run(f"test -f {APP}/{f}", timeout=60, check=False)
            checks.require(f"file.{Path(f).name}", f"{f} present in the bundle", r.returncode == 0)
        # The user would now click "Open" in Gatekeeper's first-launch dialog; this is the same approval.
        guest.run(f"xattr -dr com.apple.quarantine {APP}", timeout=120)
        cli = f"{APP}/Contents/MacOS/labrador-cli"
        r = guest.run(f"{cli} --help", timeout=60, check=False, log_to=log_to)
        checks.require("cli_runs", "labrador-cli --help runs", r.returncode == 0, r.stderr.strip()[-300:])
        log("macos: installed into ~/Applications")
        return Installed(labrador=f"{APP}/Contents/MacOS/labrador", labrador_cli=cli)
