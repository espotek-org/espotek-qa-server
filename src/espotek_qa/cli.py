"""`qa`: the QA tester's command line."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .config import PATHS, load_config, log


def cmd_run(args: argparse.Namespace) -> int:
    from .release import fetch_release
    from .runner import run_release

    cfg = load_config()
    release = fetch_release(cfg.release_repo, args.tag or cfg.release_tag)
    run = run_release(cfg, release, target_ids=args.targets.split(",") if args.targets else None)
    print(run.dir)
    return 0 if run.summary["result"] == "pass" else 1


def cmd_poll(args: argparse.Namespace) -> int:
    """Test the release if it changed since the last tested one (launchd runs this)."""
    from .release import fetch_release
    from .runner import run_release, write_json

    cfg = load_config()
    release = fetch_release(cfg.release_repo, cfg.release_tag)
    state_file = PATHS.state / "last_tested.json"
    if state_file.exists() and json.loads(state_file.read_text())["fingerprint"] == release.fingerprint:
        log(f"{release.tag} @ {release.commit[:12]} (fingerprint {release.fingerprint}) already tested")
        return 0
    log(f"new release: {release.tag} @ {release.commit[:12]} (fingerprint {release.fingerprint})")
    run = run_release(cfg, release, target_ids=None)
    PATHS.state.mkdir(parents=True, exist_ok=True)
    write_json(state_file, {"fingerprint": release.fingerprint, "commit": release.commit, "run_id": run.id,
                            "result": run.summary["result"]})
    return 0


def cmd_vm_build(args: argparse.Namespace) -> int:
    from .platforms.registry import platform

    if args.iso:
        if args.platform != "windows":
            raise SystemExit("--iso is only for windows")
        images = PATHS.cache / "images"
        images.mkdir(parents=True, exist_ok=True)
        src = Path(args.iso).resolve()
        if not src.name.startswith("Win11") or src.suffix != ".iso":
            raise SystemExit(f"{src.name}: expected Microsoft's Win11*.iso")
        for old in images.glob("Win11*.iso"):
            if old.name != src.name:
                log(f"removing the previous ISO {old.name}")
                old.unlink()
        if not (images / src.name).exists():
            log(f"copying {src.name} into {images}")
            shutil.copyfile(src, images / src.name)
    platform(args.platform).build_base(load_config(), force=args.force)
    return 0


def cmd_vm_shell(args: argparse.Namespace) -> int:
    """Open a fresh session for a platform (board attached) and ssh in; it is discarded on exit."""
    from .platforms.registry import platform
    from .runner import flock

    cfg = load_config()
    p = platform(args.platform)
    work = PATHS.home / "work" / f"shell-{p.name}"
    if work.exists():
        shutil.rmtree(work)
    logs = PATHS.home / "work" / f"shell-{p.name}-logs"
    logs.mkdir(parents=True, exist_ok=True)
    with flock(cfg.board_lock, "board"), p.session(cfg, work, logs) as sess:
        argv = sess.guest.ssh_argv(args.command) if args.command else sess.guest.interactive_argv()
        return subprocess.run(argv).returncode


def cmd_platforms(args: argparse.Namespace) -> int:
    from .platforms.registry import platform

    for name in load_config().platforms:
        if not args.images or platform(name).has_image:
            print(name)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from .web.app import serve

    serve(Path(args.env_file))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="qa", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="test a release now")
    p.add_argument("--tag", help="release tag (default from config.toml)")
    p.add_argument("--targets", help="comma-separated target ids (default: every target on an enabled platform)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("poll", help="test the release if it changed since the last run")
    p.set_defaults(fn=cmd_poll)

    vm = sub.add_parser("vm", help="golden VM images").add_subparsers(dest="vmcmd", required=True)
    p = vm.add_parser("build", help="build a platform's golden image")
    p.add_argument("platform")
    p.add_argument("--force", action="store_true", help="rebuild even if it exists")
    p.add_argument("--iso", help="windows only: the Win11 ISO downloaded from Microsoft's page")
    p.set_defaults(fn=cmd_vm_build)
    p = vm.add_parser("shell", help="open a throwaway session with the board attached and ssh in")
    p.add_argument("platform")
    p.add_argument("command", nargs="?", help="run this instead of an interactive shell")
    p.set_defaults(fn=cmd_vm_shell)

    p = sub.add_parser("platforms", help="print the enabled platforms")
    p.add_argument("--images", action="store_true", help="only those that run in a golden VM image")
    p.set_defaults(fn=cmd_platforms)

    p = sub.add_parser("web", help="serve the results site (runs as the qaweb account)")
    p.add_argument("--env-file", required=True, help="KEY=VALUE file with the site's settings and secrets")
    p.set_defaults(fn=cmd_web)

    args = ap.parse_args()
    sys.exit(args.fn(args))
