"""Windows guest: Windows 11 (unactivated) from Microsoft's official ISO,
installed unattended into a golden image. GUI work runs inside the
auto-logged-on desktop session through scheduled tasks (see qa-job.ps1),
because programs started from an ssh session have no visible desktop."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ..checks import Checks
from ..config import PATHS, Config, log
from ..download import download, sha256_file
from ..guest import Guest
from ..session import vm_session
from ..qemu import Vm, VmSpec, fresh_efivars
from . import Installed

REPO_ROOT = Path(__file__).resolve().parents[3]
GUEST_FILES = REPO_ROOT / "guest" / "windows"
STAGE = "qa-run"
HOME = r"C:\Users\qa"
APP_DIR = r"C:\Program Files\EspoTek Labrador"
DISK_SIZE = "80G"

OPENSSH_MSI = ("https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/"
               "OpenSSH-Win64-v10.0.0.0.msi",
               "ddec9c53864280759cf9f74791cefd387100e3946aa849a1c138a4ed1b96b7d9")
# The VM's display adapter has no OpenGL 3 driver. Mesa's llvmpipe opengl32.dll,
# placed next to labrador.exe at install time, provides GL 4.5 in software.
MESA_7Z = ("https://github.com/pal1000/mesa-dist-win/releases/download/26.3.1/mesa3d-26.3.1-release-msvc.7z",
           "64e84cf13d67dd354640bf443ed0445e6a83afd06a5729f0750b3744d633b3f1")
MESA_DLLS = ("opengl32.dll", "libgallium_wgl.dll")



def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def win_join(*parts: str) -> str:
    return "\\".join(parts)


# --- installation media -----------------------------------------------------------

ISO_HELP = ("Download the Windows 11 x64 ISO (English) in a browser from "
            "https://www.microsoft.com/software-download/windows11, then run "
            "`qa vm build windows --iso <file>`. Microsoft refuses automated ISO requests.")


def windows_iso() -> tuple[Path, str]:
    """The one Windows 11 ISO in the image cache, and its sha256. It is downloaded
    by hand from Microsoft's page (HTTPS from Microsoft is the integrity check;
    Microsoft publishes no machine-readable hash), so the hash is recorded."""
    isos = sorted((PATHS.cache / "images").glob("Win11*.iso"))
    if len(isos) != 1:
        raise FileNotFoundError(f"expected exactly one Win11*.iso in {PATHS.cache / 'images'}, found "
                                f"{[i.name for i in isos]}. {ISO_HELP}")
    log(f"windows: hashing {isos[0].name}")
    return isos[0], sha256_file(isos[0])


def mesa_dlls() -> Path:
    d = PATHS.cache / "mesa"
    if all((d / n).exists() for n in MESA_DLLS):
        return d
    archive = download(MESA_7Z[0], PATHS.cache / "images" / MESA_7Z[0].rsplit("/", 1)[1], sha256=MESA_7Z[1])
    with tempfile.TemporaryDirectory() as tmp:
        # macOS's bsdtar reads 7z archives.
        subprocess.run(["tar", "-xf", str(archive), "-C", tmp, *[f"x64/{n}" for n in MESA_DLLS]], check=True)
        d.mkdir(parents=True, exist_ok=True)
        for n in MESA_DLLS:
            shutil.copyfile(Path(tmp) / "x64" / n, d / n)
    return d


# --- the platform ----------------------------------------------------------------

class WindowsPlatform:
    name = "windows"
    has_image = True  # a golden VM image built by `qa vm build`
    ssh_port = 2223
    boot_timeout_s = 600.0

    def guest(self) -> Guest:
        return Guest(self.name, self.ssh_port)

    def stage_dir(self) -> str:
        return STAGE

    def abs_path(self, *parts: str) -> str:
        return win_join(HOME, STAGE, *parts)

    def mkdir_stage_cmd(self) -> str:
        p = ps_quote(self.abs_path())
        return f"if (Test-Path {p}) {{ Remove-Item -Recurse -Force {p} }}; New-Item -ItemType Directory {p} | Out-Null"

    def file_exists_cmd(self, path: str) -> str:
        return f"if (Test-Path -PathType Leaf {ps_quote(path)}) {{ exit 0 }} else {{ exit 1 }}"

    def _spec(self, name: str, disk: Path, efivars: Path, *, extra: list[Path], usb: bool,
              vnc: int | None = None) -> VmSpec:
        return VmSpec(name=name, disk=disk, efivars=efivars, ssh_port=self.ssh_port, nic="e1000e",
                      disk_device="nvme", vga="std", extra_drives=extra, usb_passthrough=usb, vnc_display=vnc)

    def session(self, cfg: Config, work: Path, logs: Path):
        return vm_session(cfg, self, work, logs)

    def run_spec(self, cfg: Config, disk: Path, efivars: Path) -> VmSpec:
        return self._spec("qa-windows", disk, efivars, extra=[], usb=True)

    # --- jobs in the desktop session ----------------------------------------------

    def start_job(self, guest: Guest, task: str, argv: list[str], log_path: str, exit_file: str) -> None:
        job_file = r"C:\qa\job-user.json" if task == "QA-User" else r"C:\qa\job-elevated.json"
        job = {"argv": argv, "cwd": self.abs_path(), "log": log_path, "exit_file": exit_file}
        b64 = base64.b64encode(json.dumps(job).encode()).decode()
        guest.run(
            f"Remove-Item -Force -ErrorAction Ignore {ps_quote(exit_file)}; "
            f"[IO.File]::WriteAllText({ps_quote(job_file)}, "
            f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))); "
            f"Start-ScheduledTask -TaskName {task}",
            timeout=60,
        )

    def job_exit_code_or_none(self, guest: Guest, exit_file: str) -> int | None:
        r = guest.run(f"if (Test-Path {ps_quote(exit_file)}) {{ Get-Content {ps_quote(exit_file)} }}", timeout=60)
        text = r.stdout.strip()
        return int(text) if text else None

    def run_job(self, guest: Guest, task: str, argv: list[str], log_path: str, *, timeout: float,
                snap=None) -> int:
        exit_file = log_path + ".exit"
        self.start_job(guest, task, argv, log_path, exit_file)
        t0 = time.monotonic()
        last_snap = 0.0
        while True:
            code = self.job_exit_code_or_none(guest, exit_file)
            if code is not None:
                return code
            now = time.monotonic()
            if now - t0 > timeout:
                raise TimeoutError(f"windows: job {argv[0]} still running after {timeout:.0f}s")
            if snap is not None and now - last_snap > 20:
                snap(f"waiting for {Path(argv[0]).name}")
                last_snap = now
            time.sleep(5)

    def start_auto_qa(self, guest: Guest, argv: list[str]) -> None:
        # In the desktop session, non-elevated: how a user runs the app.
        self.start_job(guest, "QA-User", argv, self.abs_path("auto-qa.log"), self.abs_path("exit_code"))

    def exit_code_or_none(self, guest: Guest) -> int | None:
        return self.job_exit_code_or_none(guest, self.abs_path("exit_code"))

    def last_line(self, guest: Guest, path: str) -> str:
        p = ps_quote(path)
        return guest.run(f"if (Test-Path {p}) {{ Get-Content -Tail 1 {p} }}", timeout=60).stdout.strip()

    def stop_auto_qa(self, guest: Guest) -> None:
        guest.run("Get-Process labrador -ErrorAction Ignore | Stop-Process -Force", timeout=60, check=False)

    # --- golden image ----------------------------------------------------------------

    def _config_iso(self, cfg: Config, dest: Path) -> None:
        msi = download(OPENSSH_MSI[0], PATHS.cache / "images" / OPENSSH_MSI[0].rsplit("/", 1)[1],
                       sha256=OPENSSH_MSI[1])
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            xml = (GUEST_FILES / "autounattend.xml").read_text().replace("{{PASSWORD}}", cfg.windows_password)
            (t / "autounattend.xml").write_text(xml)
            shutil.copyfile(GUEST_FILES / "qa-firstlogon.ps1", t / "qa-firstlogon.ps1")
            shutil.copyfile(PATHS.ssh_key.with_suffix(".pub"), t / "authorized_keys")
            shutil.copyfile(msi, t / msi.name)
            if dest.exists():
                dest.unlink()
            subprocess.run(["hdiutil", "makehybrid", "-quiet", "-o", str(dest), "-iso", "-joliet",
                            "-default-volume-name", "QACONFIG", str(t)], check=True)

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

        iso, iso_sha = windows_iso()
        mesa = mesa_dlls()
        config_iso = workdir / "config.iso"
        self._config_iso(cfg, config_iso)
        building = d / "base.building.qcow2"
        subprocess.run([str(cfg.qemu_prefix / "bin" / "qemu-img"), "create", "-q", "-f", "qcow2",
                        str(building), DISK_SIZE], check=True)
        efivars = d / "efivars.building.fd"
        fresh_efivars(cfg, efivars)

        vm = Vm(cfg, self._spec("qa-windows-build", building, efivars, extra=[iso, config_iso], usb=False, vnc=1),
                workdir)
        g = self.guest()
        vm.start()
        try:
            log("windows: VNC on 127.0.0.1:5901 to watch the install")
            # Windows' UEFI boot loader waits for "Press any key to boot from CD or DVD".
            for _ in range(40):
                vm.send_keys(["spc"])
                time.sleep(0.5)
            log("windows: unattended install running (expect 30-90 minutes on this hardware)")
            t0 = time.monotonic()
            n = 0
            while True:
                if not vm.running():
                    raise RuntimeError("windows: VM exited during install")
                probe = subprocess.run(g.ssh_argv("echo ok"), capture_output=True, text=True, timeout=60)
                if probe.returncode == 0:
                    break
                elapsed = time.monotonic() - t0
                if elapsed > 3 * 3600:
                    raise TimeoutError("windows: no ssh 3 h into the install")
                vm.screendump(workdir / f"install-{n:03d}.png")
                n += 1
                log(f"windows: installing, {elapsed / 60:.0f} min (latest screen: {workdir / f'install-{n - 1:03d}.png'})")
                time.sleep(60)
            log(f"windows: ssh up after {(time.monotonic() - t0) / 60:.0f} min; provisioning")
            g.run(r"New-Item -ItemType Directory -Force C:\qa\mesa | Out-Null", timeout=60)
            g.put(GUEST_FILES / "provision.ps1", "/C:/qa/provision.ps1")
            g.put(GUEST_FILES / "qa-job.ps1", "/C:/qa/qa-job.ps1")
            for dll in MESA_DLLS:
                g.put(mesa / dll, f"/C:/qa/mesa/{dll}")
            r = g.run(rf"powershell -NoProfile -ExecutionPolicy Bypass -File C:\qa\provision.ps1 "
                      rf"-Password {ps_quote(cfg.windows_password)}",
                      timeout=3600, check=False, log_to=workdir / "provision.log")
            if r.returncode != 0 or "provisioned" not in r.stdout:
                raise RuntimeError(f"windows: provisioning failed (exit {r.returncode}); see {workdir / 'provision.log'}")
            self._verify_desktop_jobs(g, vm, workdir)
            log("windows: shutting down the golden image")
            g.run("Stop-Computer -Force", timeout=60, check=False)
            if not vm.wait_exit(300):
                raise TimeoutError("windows: guest did not power off after Stop-Computer")
        finally:
            vm.kill()

        building.rename(base)
        efivars.rename(d / "efivars.fd")
        (d / "base.json").write_text(json.dumps({
            "platform": self.name, "iso": iso.name, "iso_sha256": iso_sha,
            "openssh_msi": OPENSSH_MSI[0], "mesa": MESA_7Z[0],
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }, indent=2) + "\n")
        log(f"windows: golden image ready at {base}")

    def _verify_desktop_jobs(self, g: Guest, vm: Vm, workdir: Path) -> None:
        """Both scheduled tasks must run a program inside the desktop session."""
        g.run(self.mkdir_stage_cmd(), timeout=60)
        for task in ("QA-User", "QA-Elevated"):
            out = self.abs_path(f"{task}.txt")
            code = self.run_job(g, task, [r"C:\Windows\System32\whoami.exe", "/groups"], out, timeout=300)
            text = g.run(f"Get-Content {ps_quote(out)}", timeout=60).stdout
            (workdir / f"{task}.txt").write_text(text)
            if code != 0:
                raise RuntimeError(f"windows: {task} job exited {code}: {text[-500:]}")
            if "S-1-5-4 " not in text and "INTERACTIVE" not in text:
                raise RuntimeError(f"windows: {task} did not run in the interactive session:\n{text}")
            elevated = "S-1-16-12288" in text  # High Mandatory Level
            if elevated != (task == "QA-Elevated"):
                raise RuntimeError(f"windows: {task} ran with the wrong elevation:\n{text}")
        vm.screendump(workdir / "desktop.png")


def _device_status(guest: Guest) -> str:
    return guest.run(
        "Get-PnpDevice -PresentOnly | Where-Object InstanceId -like 'USB\\VID_03EB*' | "
        "ForEach-Object { '{0} | {1} | {2} | {3}' -f $_.InstanceId, $_.Status, $_.FriendlyName, "
        "(Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_Service).Data }",
        timeout=120, check=False).stdout.strip()


class WindowsTarget:
    id = "windows"
    asset_name = "Labrador-for-Windows.exe"

    def __init__(self, platform: WindowsPlatform):
        self.platform = platform

    def install(self, guest: Guest, asset: Path, checks: Checks, log_to: Path, snap) -> Installed:
        p = self.platform
        before = _device_status(guest)
        checks.require("board_visible", "Board visible to the guest over USB passthrough", "VID_03EB" in before, before)
        guest.put(asset, f"{STAGE}/{asset.name}")
        inno_log = p.abs_path("inno-install.log")
        # Run the installer in the desktop session, elevated, so any driver-trust
        # prompt appears on screen (and in the VM screenshots) as it would for a user.
        code = p.run_job(guest, "QA-Elevated",
                         [p.abs_path(asset.name), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={inno_log}"],
                         p.abs_path("installer-output.log"), timeout=1200, snap=snap)
        guest.get(f"{STAGE}/inno-install.log", log_to.with_name("inno-install.log"))
        checks.require("installer_exit", "Installer finishes silently with exit code 0", code == 0, f"exit code {code}",
                       [{"path": "logs/inno-install.log", "kind": "log", "caption": "Inno Setup log"}])
        for f in ("labrador.exe", "labrador-cli.exe", r"assets\firmware\labrafirm_000C_03.hex"):
            r = guest.run(p.file_exists_cmd(win_join(APP_DIR, f)), timeout=60, check=False)
            checks.require(f"file.{f.replace(chr(92), '.')}", f"{f} installed", r.returncode == 0)
        drivers = guest.run("pnputil /enum-drivers", timeout=120, log_to=log_to).stdout.lower()
        for inf in ("labrador.inf", "atxmega32a4u.inf", "usbgobindar_dummy_device.inf"):
            checks.record(f"driver_store.{inf}", f"Driver package {inf} staged in the driver store", inf in drivers)
        # Give PnP a moment to bind the newly staged driver to the attached board.
        deadline = time.monotonic() + 90
        while True:
            after = _device_status(guest)
            bound = any("BA94" in line.upper() and "| OK |" in line for line in after.splitlines())
            if bound or time.monotonic() > deadline:
                break
            time.sleep(5)
        checks.record("driver_bound", "Board (03eb:ba94) bound to a working driver after install", bound, after)
        r = guest.run(r"Test-Path 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\EspoTek Labrador Unified App (Beta)'",
                      timeout=60)
        checks.record("start_menu", "Start menu group created", r.stdout.strip() == "True")
        for dll in MESA_DLLS:
            guest.run(f"Copy-Item {ps_quote(win_join(r'C:\qa\mesa', dll))} {ps_quote(win_join(APP_DIR, dll))}", timeout=60)
        checks.record("env.software_gl", "Test environment: Mesa llvmpipe opengl32.dll placed next to labrador.exe "
                      "(the VM has no GPU driver; real installs use the system's GL)", True)
        cli = win_join(APP_DIR, "labrador-cli.exe")
        r = guest.run(f"& {ps_quote(cli)} --help; exit $LASTEXITCODE", timeout=60, check=False, log_to=log_to)
        checks.require("cli_runs", "labrador-cli.exe --help runs", r.returncode == 0, r.stderr.strip()[-300:])
        return Installed(labrador=win_join(APP_DIR, "labrador.exe"), labrador_cli=cli)
