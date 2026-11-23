"""One QA run: a release × every enabled target, sequentially. Each target gets
a fresh session (a throwaway VM, or the reset qamac account), installs the
release asset the way a user would, then runs the installed app's own QA:

    labrador --auto-qa <out_dir>

which writes <out_dir>/report.json (and screenshots/plots) and exits 0 iff
every test passed. The server merges its install checks into that report."""

from __future__ import annotations

import fcntl
import json
import os
import time
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checks import Checks, InstallFailed
from .config import PATHS, Config, log
from .platforms import Target
from .platforms.registry import targets_for
from .release import Release, fetch_asset
from .session import Session

AUTO_QA_TIMEOUT_S = 2 * 3600
# A build that predates --auto-qa ignores the flag and just opens its window,
# so no progress.log by then means the build doesn't support it.
AUTO_QA_STARTUP_S = 120
POLL_S = 10
SCREEN_EVERY_S = 20


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o644)
    tmp.rename(path)


@contextmanager
def flock(path: Path, what: str):
    """Exclusive flock(2) on a shared lock file, waiting with progress."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        t0 = time.monotonic()
        last = 0.0
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.monotonic() - t0
                if waited - last >= 30:
                    log(f"waiting for the {what} lock ({path}), {waited:.0f}s so far")
                    last = waited
                time.sleep(2)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Run:
    def __init__(self, cfg: Config, release: Release, targets: list[Target]):
        self.cfg = cfg
        self.release = release
        self.targets = targets
        self.id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{release.commit[:7]}"
        self.dir = PATHS.results / self.id
        self.dir.mkdir(parents=True)
        os.chmod(self.dir, 0o755)
        self.summary: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.id,
            "status": "running",
            "result": None,
            "started_at": now_iso(),
            "finished_at": None,
            "release": release.to_json(),
            "host": os.uname().nodename,
            "targets": [
                {"id": t.id, "platform": t.platform.name, "asset": t.asset_name, "status": "pending",
                 "result": None, "counts": None, "message": "", "started_at": None, "finished_at": None,
                 "report": f"{t.id}/report.json"}
                for t in targets
            ],
        }
        self.save()

    def save(self) -> None:
        write_json(self.dir / "run.json", self.summary)
        update_index()

    def _entry(self, target: Target) -> dict:
        return next(e for e in self.summary["targets"] if e["id"] == target.id)

    def execute(self) -> bool:
        log(f"run {self.id}: release {self.release.tag} @ {self.release.commit[:12]}, "
            f"targets {[t.id for t in self.targets]}")
        for i, target in enumerate(self.targets, 1):
            entry = self._entry(target)
            entry["status"] = "running"
            entry["started_at"] = now_iso()
            self.save()
            log(f"[{i}/{len(self.targets)}] {target.id}")
            result, counts, message = self._run_target(target)
            entry.update(status="done", result=result, counts=counts, message=message, finished_at=now_iso())
            self.save()
        ok = all(e["result"] == "pass" for e in self.summary["targets"])
        self.summary.update(status="done", result="pass" if ok else "fail", finished_at=now_iso())
        self.save()
        log(f"run {self.id}: {self.summary['result'].upper()}")
        return ok

    def _run_target(self, target: Target) -> tuple[str, dict | None, str]:
        tdir = self.dir / target.id
        (tdir / "logs").mkdir(parents=True)
        checks = Checks("install")
        try:
            return self._run_target_in_session(target, tdir, checks)
        except Exception as e:  # this target broke: record it in its report and carry on with the next target
            message = f"{type(e).__name__}: {e}"
            (tdir / "logs" / "error.txt").write_text(traceback.format_exc())
            log(f"{target.id}: ERROR {message}")
            report = write_target_report(tdir, target, checks, auto_qa_report=None,
                                         problem=f"the run broke before --auto-qa finished: {message}")
            return "error", report["counts"], message

    def _run_target_in_session(self, target: Target, tdir: Path, checks: Checks) -> tuple[str, dict | None, str]:
        platform = target.platform
        logs = tdir / "logs"
        asset = fetch_asset(self.release, target.asset_name)
        work = PATHS.home / "work" / self.id / target.id
        with flock(self.cfg.board_lock, "board"), platform.session(self.cfg, work, logs) as sess:
            screens = Screens(tdir / "vm-screens")  # timestamps count from the session being ready
            guest = sess.guest
            screens.take(sess, "session ready")
            stage = platform.stage_dir()
            guest.run(platform.mkdir_stage_cmd(), timeout=60)
            try:
                installed = target.install(guest, asset, checks, logs / "install.log",
                                           lambda caption: screens.take(sess, caption))
            except InstallFailed as e:
                screens.take(sess, "install failed")
                write_target_report(tdir, target, checks, auto_qa_report=None, problem=f"--auto-qa not run: {e}")
                return "fail", counts_of(checks.items), str(e)
            screens.take(sess, "installed")

            argv = [installed.labrador, "--auto-qa", platform.abs_path("out")]
            (logs / "auto-qa.argv").write_text(json.dumps(argv) + "\n")
            platform.start_auto_qa(guest, argv)
            outcome, exit_code = self._watch(target, sess, screens)
            screens.take(sess, f"--auto-qa {outcome}" + (f" (exit {exit_code})" if exit_code is not None else ""))

            for name in ("auto-qa.log",):
                if guest.run(platform.file_exists_cmd(platform.abs_path(name)), timeout=60,
                             check=False).returncode == 0:
                    guest.get(f"{stage}/{name}", logs / name)
            have_out = guest.run(platform.file_exists_cmd(platform.abs_path("out", "progress.log")),
                                 timeout=60, check=False).returncode == 0
            if have_out:
                guest.get(f"{stage}/out", tdir / "auto-qa")
            have_report = (tdir / "auto-qa" / "report.json").exists()

        if outcome == "never started":
            problem = (f"this build doesn't support --auto-qa: it wrote no progress.log within "
                       f"{AUTO_QA_STARTUP_S}s (builds before --auto-qa ignore the flag and just open their window)")
        elif outcome == "timed out":
            problem = f"--auto-qa did not finish within {AUTO_QA_TIMEOUT_S / 3600:.0f} h"
        elif not have_report:
            problem = f"--auto-qa exited {exit_code} without writing report.json"
        else:
            problem = None
        auto_qa_report = json.loads((tdir / "auto-qa" / "report.json").read_text()) if have_report else None
        report = write_target_report(tdir, target, checks, auto_qa_report=auto_qa_report, problem=problem)
        if problem is not None:
            return "error", report["counts"], problem
        assert auto_qa_report is not None
        if (auto_qa_report["result"] == "pass") != (exit_code == 0):
            return "error", report["counts"], (f"--auto-qa exit code {exit_code} disagrees with its report.json "
                                               f"result {auto_qa_report['result']!r}")
        return report["result"], report["counts"], ""

    def _watch(self, target: Target, sess: Session, screens: "Screens") -> tuple[str, int | None]:
        """Follow --auto-qa until it exits. Returns ("exited", code), ("never started", None)
        or ("timed out", None)."""
        platform = target.platform
        guest = sess.guest
        progress = platform.abs_path("out", "progress.log")
        t0 = time.monotonic()
        last_screen = 0.0
        last_tail = ""
        started = False
        while True:
            if not sess.alive():
                raise RuntimeError(f"{target.id}: the VM exited while --auto-qa was running")
            code = platform.exit_code_or_none(guest)
            if code is not None:
                log(f"{target.id}: --auto-qa exited {code} after {time.monotonic() - t0:.0f}s")
                return "exited", code
            tail = platform.last_line(guest, progress)
            if tail:
                started = True
                if tail != last_tail:
                    log(f"{target.id}: {tail[:200]}")
                    last_tail = tail
            now = time.monotonic()
            if now - last_screen >= SCREEN_EVERY_S:
                screens.take(sess, tail[:200])
                last_screen = now
            if not started and now - t0 > AUTO_QA_STARTUP_S:
                log(f"{target.id}: no progress.log after {AUTO_QA_STARTUP_S}s; this build lacks --auto-qa")
                screens.take(sess, "no --auto-qa progress; stopping the app")
                platform.stop_auto_qa(guest)
                return "never started", None
            if now - t0 > AUTO_QA_TIMEOUT_S:
                log(f"{target.id}: --auto-qa timed out; stopping it")
                platform.stop_auto_qa(guest)
                return "timed out", None
            time.sleep(POLL_S)


def counts_of(tests: list[dict]) -> dict:
    counts = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    for t in tests:
        counts[t["status"]] += 1
    return counts


def write_target_report(tdir: Path, target: Target, checks: Checks, *, auto_qa_report: dict | None,
                        problem: str | None) -> dict:
    """Merge the server's install checks and the app's --auto-qa report into <target>/report.json."""
    tests = list(checks.items)
    screenshots: list[dict] = []
    if auto_qa_report is not None:
        if auto_qa_report["schema_version"] != 1:
            raise ValueError(f"unsupported --auto-qa report schema_version {auto_qa_report['schema_version']}")
        for t in auto_qa_report["tests"]:
            t = dict(t)
            t["artifacts"] = [dict(a, path=f"auto-qa/{a['path']}") for a in t["artifacts"]]
            tests.append(t)
        screenshots = [dict(s, path=f"auto-qa/{s['path']}") for s in auto_qa_report["screenshots"]]
    if problem is not None:
        tests.append({"id": "auto_qa.run", "group": "auto_qa", "title": "labrador --auto-qa ran to completion",
                      "status": "error", "duration_s": 0, "message": problem, "measurements": [],
                      "artifacts": [{"path": "logs/auto-qa.log", "kind": "log", "caption": "app stdout/stderr"},
                                    {"path": "auto-qa/progress.log", "kind": "log", "caption": "--auto-qa progress"}]})
    counts = counts_of(tests)
    report = {
        "schema_version": 1,
        "target": target.id,
        "platform": target.platform.name,
        "asset": target.asset_name,
        "result": "pass" if counts["fail"] == 0 and counts["error"] == 0 else "fail",
        "counts": counts,
        "auto_qa": auto_qa_report and {k: auto_qa_report[k] for k in
                                       ("suite", "app", "platform", "started_at", "finished_at", "result")},
        "tests": tests,
        "screenshots": screenshots,
        "vm_screens": "vm-screens/index.json",
    }
    write_json(tdir / "report.json", report)
    return report


class Screens:
    """Host-side screendumps of the guest display, captioned with --auto-qa's latest progress line."""

    def __init__(self, d: Path):
        self.dir = d
        self.items: list[dict] = []
        self.t0 = time.monotonic()
        d.mkdir(parents=True, exist_ok=True)

    def take(self, sess: Session, caption: str) -> None:
        if sess.screendump is None or not sess.alive():
            return
        name = f"{len(self.items):04d}.png"
        sess.screendump(self.dir / name)
        self.items.append({"path": f"vm-screens/{name}", "t_s": round(time.monotonic() - self.t0, 1),
                           "caption": caption})
        write_json(self.dir / "index.json", {"screens": self.items})


def run_release(cfg: Config, release: Release, *, target_ids: list[str] | None) -> Run:
    targets = targets_for(cfg, target_ids)
    with flock(PATHS.run_lock, "run"):
        run = Run(cfg, release, targets)
        run.execute()
        return run


def update_index() -> None:
    runs = []
    for d in sorted(PATHS.results.iterdir(), reverse=True):
        f = d / "run.json"
        if d.is_dir() and f.exists():
            s = json.loads(f.read_text())
            runs.append({k: s[k] for k in ("run_id", "status", "result", "started_at", "finished_at")} |
                        {"commit": s["release"]["commit"], "tag": s["release"]["tag"],
                         "targets": [{k: t[k] for k in ("id", "result", "counts")} for t in s["targets"]]})
    write_json(PATHS.results / "index.json", {"runs": runs})
