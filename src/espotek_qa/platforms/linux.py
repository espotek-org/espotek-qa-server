"""Linux guest: Ubuntu 24.04 LTS cloud image, provisioned once by cloud-init into
a golden image with an auto-login X session (openbox) and Mesa llvmpipe GL."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..checks import Checks
from ..config import PATHS, Config, log
from ..download import download
from ..guest import Guest
from ..session import vm_session
from ..qemu import Vm, VmSpec, fresh_efivars, qemu_img
from . import Installed

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGE_DIR_URL = "https://cloud-images.ubuntu.com/noble/current"
IMAGE_NAME = "noble-server-cloudimg-amd64.img"
DISK_SIZE = "40G"
STAGE = "qa-run"


def _image_sha256() -> str:
    sums = httpx.get(f"{IMAGE_DIR_URL}/SHA256SUMS", timeout=30, follow_redirects=True)
    sums.raise_for_status()
    for line in sums.text.splitlines():
        digest, name = line.split(maxsplit=1)
        if name.lstrip("*") == IMAGE_NAME:
            return digest
    raise ValueError(f"{IMAGE_NAME} not listed in {IMAGE_DIR_URL}/SHA256SUMS")


def _seed_iso(dest: Path) -> None:
    pubkey = PATHS.ssh_key.with_suffix(".pub").read_text().strip()
    user_data = (REPO_ROOT / "guest" / "linux" / "user-data").read_text().replace("{{SSH_PUBKEY}}", pubkey)
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "user-data").write_text(user_data)
        (t / "meta-data").write_text(f"instance-id: qa-linux-{int(time.time())}\nlocal-hostname: qa-linux\n")
        if dest.exists():
            dest.unlink()
        subprocess.run(
            ["hdiutil", "makehybrid", "-quiet", "-o", str(dest), "-iso", "-joliet",
             "-default-volume-name", "cidata", str(t)],
            check=True,
        )


class LinuxPlatform:
    name = "linux"
    has_image = True  # a golden VM image built by `qa vm build`
    ssh_port = 2222
    boot_timeout_s = 300.0

    def guest(self) -> Guest:
        return Guest(self.name, self.ssh_port)

    def stage_dir(self) -> str:
        return STAGE

    def abs_path(self, *parts: str) -> str:
        return "/".join([f"/home/qa/{STAGE}", *parts])

    def mkdir_stage_cmd(self) -> str:
        return f"rm -rf ~/{STAGE} && mkdir -p ~/{STAGE}"

    def file_exists_cmd(self, path: str) -> str:
        return f"test -f {shlex.quote(path)}"

    def _spec(self, name: str, disk: Path, efivars: Path, *, extra: list[Path], usb: bool) -> VmSpec:
        return VmSpec(name=name, disk=disk, efivars=efivars, ssh_port=self.ssh_port, nic="virtio-net-pci",
                      disk_device="virtio", vga="virtio", extra_drives=extra, usb_passthrough=usb)

    def session(self, cfg: Config, work: Path, logs: Path):
        return vm_session(cfg, self, work, logs)

    def run_spec(self, cfg: Config, disk: Path, efivars: Path) -> VmSpec:
        return self._spec("qa-linux", disk, efivars, extra=[], usb=True)

    def build_base(self, cfg: Config, *, force: bool) -> None:
        d = PATHS.vm_dir(self.name)
        base = d / "base.qcow2"
        if base.exists() and not force:
            raise FileExistsError(f"{base} exists; pass --force to rebuild it")
        d.mkdir(parents=True, exist_ok=True)
        workdir = d / "build"
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir()

        sha = _image_sha256()
        image = download(f"{IMAGE_DIR_URL}/{IMAGE_NAME}", PATHS.cache / "images" / IMAGE_NAME, sha256=sha)
        building = d / "base.building.qcow2"
        log("linux: converting cloud image to a qcow2 golden image")
        subprocess.run([str(qemu_img(cfg)), "convert", "-O", "qcow2", str(image), str(building)], check=True)
        subprocess.run([str(qemu_img(cfg)), "resize", "-q", str(building), DISK_SIZE], check=True)
        seed = workdir / "seed.iso"
        _seed_iso(seed)
        efivars = d / "efivars.building.fd"
        fresh_efivars(cfg, efivars)

        vm = Vm(cfg, self._spec("qa-linux-build", building, efivars, extra=[seed], usb=False), workdir)
        g = self.guest()
        vm.start()
        try:
            g.wait_ssh(1800, alive=vm.running)
            log("linux: waiting for cloud-init to finish provisioning (package installs take a while)")
            status = g.run("cloud-init status --wait --long", timeout=5400, check=False, log_to=workdir / "provision.log")
            if status.returncode != 0:
                g.run("sudo cat /var/log/cloud-init-output.log", timeout=60, check=False, log_to=workdir / "provision.log")
                raise RuntimeError(f"linux: cloud-init failed (exit {status.returncode}); see {workdir / 'provision.log'}")
            # Later boots have no seed; stop cloud-init looking for one.
            g.run("sudo touch /etc/cloud/cloud-init.disabled", timeout=60)
            self._verify_desktop(g, vm, workdir)
            vm.shutdown()
        finally:
            vm.kill()

        building.rename(base)
        efivars.rename(d / "efivars.fd")
        (d / "base.json").write_text(json.dumps({
            "platform": self.name,
            "image": IMAGE_NAME,
            "image_sha256": sha,
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }, indent=2) + "\n")
        log(f"linux: golden image ready at {base}")

    def _verify_desktop(self, g: Guest, vm: Vm, workdir: Path) -> None:
        g.run("sudo systemctl restart lightdm", timeout=60)
        deadline = time.monotonic() + 120
        while True:
            probe = g.run("DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority glxinfo -B", timeout=60, check=False)
            if probe.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"linux: auto-login X session never came up: {probe.stderr[-500:]}")
            time.sleep(5)
        (workdir / "glxinfo.txt").write_text(probe.stdout)
        if "OpenGL core profile version string: 4." not in probe.stdout:
            raise RuntimeError(f"linux: expected a GL 4.x core profile from llvmpipe, got:\n{probe.stdout}")
        vm.screendump(workdir / "desktop.png")

    # --- running --auto-qa ----------------------------------------------------

    def start_auto_qa(self, guest: Guest, argv: list[str]) -> None:
        cmd = " ".join(shlex.quote(a) for a in argv)
        inner = (
            f"cd ~/{STAGE} && DISPLAY=:0 XAUTHORITY=$HOME/.Xauthority {cmd} > ~/{STAGE}/auto-qa.log 2>&1; "
            f"echo $? > ~/{STAGE}/exit_code.tmp && mv ~/{STAGE}/exit_code.tmp ~/{STAGE}/exit_code"
        )
        guest.run(f"nohup sh -c {shlex.quote(inner)} > /dev/null 2>&1 &", timeout=60)

    def exit_code_or_none(self, guest: Guest) -> int | None:
        text = guest.run(f"cat ~/{STAGE}/exit_code 2>/dev/null || true", timeout=60).stdout.strip()
        return int(text) if text else None

    def last_line(self, guest: Guest, path: str) -> str:
        return guest.run(f"tail -n 1 {shlex.quote(path)} 2>/dev/null || true", timeout=60).stdout.strip()

    def stop_auto_qa(self, guest: Guest) -> None:
        # Match the app by its argument (the AppImage runs as AppRun.wrapped inside its mount). The
        # [a] keeps the pattern from matching the shell this command itself runs in.
        guest.run("pkill -f -- '--auto-q[a]'; true", timeout=60, check=False)


def _lsusb_has_board(guest: Guest) -> tuple[bool, str]:
    r = guest.run("lsusb -d 03eb:", timeout=60, check=False)
    return r.returncode == 0 and "03eb:" in r.stdout, r.stdout.strip()


class LinuxDebTarget:
    id = "linux-deb"
    asset_name = "Labrador-for-Linux-x86_64.deb"

    def __init__(self, platform: LinuxPlatform):
        self.platform = platform

    def install(self, guest: Guest, asset: Path, checks: Checks, log_to: Path, snap) -> Installed:
        ok, usb = _lsusb_has_board(guest)
        checks.require("board_visible", "Board visible to the guest over USB passthrough", ok, usb)
        guest.put(asset, f"{STAGE}/{asset.name}")
        pkg = guest.run(f"dpkg-deb -f ~/{STAGE}/{asset.name} Package Version", timeout=60).stdout.strip()
        r = guest.run(f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ~/{STAGE}/{asset.name}",
                      timeout=900, check=False, log_to=log_to)
        checks.require("apt_install", f"apt installs the .deb ({pkg.replace(chr(10), ' ')})", r.returncode == 0,
                       f"apt-get exit {r.returncode}: {r.stderr.strip()[-400:]}")
        for path in ("/usr/bin/labrador", "/usr/bin/labrador-cli"):
            r = guest.run(f"test -x {path}", timeout=60, check=False)
            checks.require(f"file{path.replace('/', '.')}", f"{path} installed and executable", r.returncode == 0)
        r = guest.run("ls /usr/lib/udev/rules.d/69-labrador.rules /lib/udev/rules.d/69-labrador.rules "
                      "/etc/udev/rules.d/69-labrador.rules 2>/dev/null", timeout=60, check=False)
        checks.record("udev_rule", "udev rule 69-labrador.rules installed", bool(r.stdout.strip()), r.stdout.strip())
        r = guest.run("grep -l -i labrador /usr/share/applications/*.desktop", timeout=60, check=False)
        checks.record("desktop_entry", "Desktop menu entry installed", r.returncode == 0, r.stdout.strip())
        # The rule must make the board usable without root.
        r = guest.run("for d in $(lsusb -d 03eb: | awk '{print \"/dev/bus/usb/\"$2\"/\"substr($4,1,3)}'); do "
                      "test -r $d -a -w $d && echo \"$d rw\" || echo \"$d NOT rw for $(id -un)\"; done",
                      timeout=60, check=False)
        checks.record("usb_permissions", "Board's device node is read/write for a normal user",
                      "NOT" not in r.stdout and "rw" in r.stdout, r.stdout.strip())
        package = guest.run(f"dpkg-deb -f ~/{STAGE}/{asset.name} Package", timeout=60).stdout.strip()
        self._check_dependency_closure(guest, package, checks, log_to)
        r = guest.run("labrador-cli --help", timeout=60, check=False, log_to=log_to)
        checks.require("cli_runs", "labrador-cli --help runs", r.returncode == 0, r.stderr.strip()[-300:])
        return Installed(labrador="/usr/bin/labrador", labrador_cli="/usr/bin/labrador-cli")

    def _check_dependency_closure(self, guest: Guest, package: str, checks: Checks, log_to: Path) -> None:
        """Every shared library the installed binaries load must come from a package the .deb
        depends on (directly or transitively). Otherwise the app only works on machines that
        happen to have that package, whatever this image contains."""
        closure = guest.run(
            f"apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks "
            f"--no-replaces --no-enhances {shlex.quote(package)} | grep -v '^ ' | sed 's/:any$//' | sort -u",
            timeout=120, log_to=log_to).stdout.split()
        if package not in closure:
            raise RuntimeError(f"apt-cache knows no installed package {package!r}; got {closure[:10]}")
        owners = guest.run(
            "for b in /usr/bin/labrador /usr/bin/labrador-cli; do ldd \"$(readlink -f $b)\"; done "
            "| awk '/=>/ {print $1, $3}' | sort -u | while read name path; do "
            "if [ \"$path\" = not ]; then echo \"$name NOT-FOUND\"; continue; fi; "
            "real=$(readlink -f \"$path\"); "
            "owner=$(dpkg -S \"$real\" 2>/dev/null || dpkg -S \"$path\" 2>/dev/null || dpkg -S \"${real#/usr}\" 2>/dev/null); "
            "echo \"$name ${owner%%:*}\"; done",
            timeout=120, log_to=log_to).stdout.strip().splitlines()
        problems = []
        for line in owners:
            lib, owner = (line.split(maxsplit=1) + [""])[:2]
            if owner == "NOT-FOUND":
                problems.append(f"{lib} is not on the system at all")
            elif owner == "":
                problems.append(f"{lib} belongs to no installed package")
            elif owner != package and owner not in closure:
                problems.append(f"{lib} comes from {owner}, which {package} does not depend on")
        checks.record("dependency_closure",
                      "Every shared library labrador and labrador-cli load comes from a declared dependency",
                      not problems and bool(owners),
                      "; ".join(problems) if problems else f"{len(owners)} libraries, all covered")


class LinuxAppImageTarget:
    id = "linux-appimage"
    asset_name = "Labrador-for-Linux-x86_64.AppImage"

    def __init__(self, platform: LinuxPlatform):
        self.platform = platform

    def install(self, guest: Guest, asset: Path, checks: Checks, log_to: Path, snap) -> Installed:
        ok, usb = _lsusb_has_board(guest)
        checks.require("board_visible", "Board visible to the guest over USB passthrough", ok, usb)
        app = f"/home/qa/{STAGE}/{asset.name}"
        cli = f"/home/qa/{STAGE}/bin/labrador-cli"
        guest.put(asset, f"{STAGE}/{asset.name}")
        guest.run(f"chmod +x {app} && mkdir -p ~/{STAGE}/bin && ln -sf {app} {cli}", timeout=60)
        # First run: the AppRun hook installs the udev rule into /run/udev/rules.d.
        r = guest.run(f"{app} cli --help", timeout=120, check=False, log_to=log_to)
        checks.require("cli_runs", "AppImage `cli --help` runs", r.returncode == 0, (r.stdout + r.stderr).strip()[-400:])
        r = guest.run(f"{cli} --help", timeout=120, check=False, log_to=log_to)
        checks.require("cli_symlink_runs", "labrador-cli symlink to the AppImage runs", r.returncode == 0,
                       (r.stdout + r.stderr).strip()[-400:])
        r = guest.run("test -f /run/udev/rules.d/69-labrador.rules", timeout=60, check=False)
        checks.record("udev_rule_first_run", "First run installed the udev rule in /run/udev/rules.d",
                      r.returncode == 0)
        return Installed(labrador=app, labrador_cli=cli)
