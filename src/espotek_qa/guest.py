"""SSH/SCP into a guest over the QEMU user-net port forward."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import PATHS, log


@dataclass(frozen=True)
class Guest:
    name: str
    port: int
    user: str = "qa"

    def _opts(self) -> list[str]:
        return [
            "-i", str(PATHS.ssh_key),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=8",
        ]

    def ssh_argv(self, command: str) -> list[str]:
        return ["ssh", *self._opts(), "-p", str(self.port), f"{self.user}@127.0.0.1", command]

    def interactive_argv(self) -> list[str]:
        return ["ssh", *self._opts(), "-t", "-p", str(self.port), f"{self.user}@127.0.0.1"]

    def run(self, command: str, *, timeout: float, check: bool = True, log_to: Path | None = None) -> subprocess.CompletedProcess:
        """Run a command in the guest. Output goes to log_to when given (and is returned)."""
        proc = subprocess.run(self.ssh_argv(command), capture_output=True, text=True, timeout=timeout)
        if log_to is not None:
            with log_to.open("a") as f:
                f.write(f"$ {command}\n{proc.stdout}{proc.stderr}[exit {proc.returncode}]\n")
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"{self.name}: `{command}` exited {proc.returncode}\nstdout: {proc.stdout[-3000:]}\nstderr: {proc.stderr[-3000:]}"
            )
        return proc

    def put(self, local: Path, remote: str, *, timeout: float = 1800) -> None:
        subprocess.run(
            ["scp", *self._opts(), "-q", "-r", "-P", str(self.port), str(local), f"{self.user}@127.0.0.1:{remote}"],
            check=True, timeout=timeout,
        )

    def get(self, remote: str, local: Path, *, timeout: float = 1800) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["scp", *self._opts(), "-q", "-r", "-P", str(self.port), f"{self.user}@127.0.0.1:{remote}", str(local)],
            check=True, timeout=timeout,
        )

    def wait_ssh(self, timeout: float, *, alive=None) -> None:
        """Poll until `ssh true` works. `alive()` lets the caller abort if the VM died."""
        deadline = time.monotonic() + timeout
        start = time.monotonic()
        last_report = 0.0
        while True:
            proc = subprocess.run(self.ssh_argv("echo ok"), capture_output=True, text=True, timeout=60)
            if proc.returncode == 0 and "ok" in proc.stdout:
                log(f"{self.name}: ssh up after {time.monotonic() - start:.0f}s")
                return
            if alive is not None and not alive():
                raise RuntimeError(f"{self.name}: VM exited while waiting for ssh")
            now = time.monotonic()
            if now > deadline:
                raise TimeoutError(f"{self.name}: no ssh after {timeout:.0f}s ({proc.stderr.strip()[-300:]})")
            if now - last_report > 30:
                log(f"{self.name}: waiting for ssh ({now - start:.0f}s)")
                last_report = now
            time.sleep(5)
