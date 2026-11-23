"""QEMU on macOS (HVF): launch a guest, talk QMP, take screendumps.

Each run boots a throwaway qcow2 overlay on top of a golden image, so the
golden image is never modified by a test run.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, log


class QmpError(RuntimeError):
    pass


class Qmp:
    """Minimal QMP client over a unix socket."""

    def __init__(self, path: Path, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                self._sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        self._file = self._sock.makefile("rwb")
        greeting = self._read()
        if "QMP" not in greeting:
            raise QmpError(f"unexpected QMP greeting: {greeting}")
        self.execute("qmp_capabilities")

    def _read(self) -> dict:
        line = self._file.readline()
        if not line:
            raise QmpError("QMP socket closed")
        return json.loads(line)

    def execute(self, command: str, **arguments) -> object:
        msg: dict = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        self._file.write(json.dumps(msg).encode() + b"\n")
        self._file.flush()
        while True:
            reply = self._read()
            if "event" in reply:
                continue
            if "error" in reply:
                raise QmpError(f"{command}: {reply['error']}")
            return reply["return"]

    def close(self) -> None:
        self._file.close()
        self._sock.close()


def qemu_img(cfg: Config) -> Path:
    return cfg.qemu_prefix / "bin" / "qemu-img"


def create_overlay(cfg: Config, base: Path, overlay: Path) -> None:
    if overlay.exists():
        overlay.unlink()
    subprocess.run(
        [str(qemu_img(cfg)), "create", "-q", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(overlay)],
        check=True,
    )


def ovmf_code(cfg: Config) -> Path:
    path = cfg.qemu_prefix / "share" / "qemu" / "edk2-x86_64-code.fd"
    if not path.exists():
        raise FileNotFoundError(f"OVMF firmware not found at {path}")
    return path


def ovmf_vars_template(cfg: Config) -> Path:
    path = cfg.qemu_prefix / "share" / "qemu" / "edk2-i386-vars.fd"
    if not path.exists():
        raise FileNotFoundError(f"OVMF vars template not found at {path}")
    return path


@dataclass
class VmSpec:
    """Everything that differs between guests; the rest is common."""

    name: str
    disk: Path
    efivars: Path
    ssh_port: int
    nic: str
    disk_device: str  # "virtio" or "nvme"
    vga: str
    extra_drives: list[Path] = field(default_factory=list)  # read-only CD images (install media, seeds)
    usb_passthrough: bool = True
    vnc_display: int | None = None  # 127.0.0.1:590N, for watching a build by eye


class Vm:
    def __init__(self, cfg: Config, spec: VmSpec, workdir: Path):
        self.cfg = cfg
        self.spec = spec
        self.workdir = workdir
        workdir.mkdir(parents=True, exist_ok=True)
        self.qmp_path = workdir / "qmp.sock"
        self.serial_log = workdir / "serial.log"
        self.stderr_log = workdir / "qemu.log"
        self.proc: subprocess.Popen | None = None
        self.qmp: Qmp | None = None

    def argv(self) -> list[str]:
        s = self.spec
        argv = [
            str(self.cfg.qemu_prefix / "bin" / "qemu-system-x86_64"),
            "-name", s.name,
            "-machine", "q35",
            "-accel", "hvf",
            "-cpu", "host",
            "-smp", str(self.cfg.vm_cpus),
            "-m", str(self.cfg.vm_memory_mb),
            "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf_code(self.cfg)}",
            "-drive", f"if=pflash,format=raw,file={s.efivars}",
            "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{s.ssh_port}-:22",
            "-device", f"{s.nic},netdev=net0",
            "-vga", s.vga,
            "-device", "qemu-xhci,id=xhci",
            "-device", "usb-tablet,bus=xhci.0",
            "-qmp", f"unix:{self.qmp_path},server=on,wait=off",
            "-serial", f"file:{self.serial_log}",
            "-rtc", "base=utc",
        ]
        if s.disk_device == "virtio":
            argv += ["-drive", f"file={s.disk},if=virtio,format=qcow2,discard=unmap"]
        elif s.disk_device == "nvme":
            argv += [
                "-drive", f"file={s.disk},if=none,id=disk0,format=qcow2,discard=unmap",
                "-device", "nvme,drive=disk0,serial=qa0",
            ]
        else:
            raise ValueError(f"unknown disk_device {s.disk_device!r}")
        for i, cd in enumerate(s.extra_drives):
            argv += [
                "-drive", f"file={cd},if=none,id=cd{i},media=cdrom,readonly=on",
                "-device", f"ide-cd,drive=cd{i},bus=ide.{i}",
            ]
        if s.usb_passthrough:
            # One usb-host per identity: the board re-enumerates as the DFU
            # bootloader while firmware is flashed and must follow into the guest.
            for vid, pid in self.cfg.usb_ids:
                argv += ["-device", f"usb-host,bus=xhci.0,vendorid={vid:#06x},productid={pid:#06x}"]
        if s.vnc_display is None:
            argv += ["-display", "none"]
        else:
            argv += ["-display", "none", "-vnc", f"127.0.0.1:{s.vnc_display}"]
        return argv

    def start(self) -> None:
        if self.qmp_path.exists():
            self.qmp_path.unlink()
        argv = self.argv()
        log(f"{self.spec.name}: starting qemu")
        (self.workdir / "qemu.argv").write_text(" ".join(argv) + "\n")
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=self.stderr_log.open("wb"))
        try:
            self.qmp = Qmp(self.qmp_path)
        except Exception:
            self.proc.poll()
            raise RuntimeError(
                f"{self.spec.name}: qemu did not come up (exit {self.proc.returncode}); "
                f"stderr: {self.stderr_log.read_text()[-2000:]}"
            )

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def screendump(self, path: Path) -> None:
        assert self.qmp is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        self.qmp.execute("screendump", filename=str(path), format="png")

    def send_keys(self, keys: list[str]) -> None:
        """Press a chord, e.g. ["ctrl", "alt", "delete"] (QKeyCode names)."""
        assert self.qmp is not None
        self.qmp.execute("send-key", keys=[{"type": "qcode", "data": k} for k in keys])

    def wait_exit(self, timeout: float) -> bool:
        assert self.proc is not None
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def shutdown(self, timeout: float = 180.0) -> None:
        """ACPI power-off; hard quit only if the guest ignores it, and say so."""
        if not self.running():
            return
        assert self.qmp is not None
        log(f"{self.spec.name}: ACPI power-off")
        self.qmp.execute("system_powerdown")
        if self.wait_exit(timeout):
            return
        log(f"{self.spec.name}: guest ignored power-off for {timeout:.0f}s; forcing quit")
        self.qmp.execute("quit")
        if not self.wait_exit(30):
            assert self.proc is not None
            self.proc.kill()
            self.proc.wait()

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def fresh_efivars(cfg: Config, dest: Path) -> None:
    shutil.copyfile(ovmf_vars_template(cfg), dest)
