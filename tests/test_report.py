import json
from pathlib import Path

from espotek_qa.checks import Checks
from espotek_qa.release import Asset, Release
from espotek_qa.runner import write_target_report


class FakePlatform:
    name = "linux"


class FakeTarget:
    id = "linux-deb"
    asset_name = "a.deb"
    platform = FakePlatform()


def auto_qa_report(result: str) -> dict:
    return {
        "schema_version": 1, "suite": {"name": "s", "commit": None}, "app": {}, "platform": {},
        "started_at": "", "finished_at": "", "result": result,
        "tests": [{"id": "hw.x", "group": "hw", "title": "x", "status": "pass" if result == "pass" else "fail",
                   "duration_s": 1, "message": "", "measurements": [],
                   "artifacts": [{"path": "plots/x.png", "kind": "png", "caption": ""}]}],
        "screenshots": [{"id": "s", "path": "screenshots/s.png", "caption": "", "expect": ""}],
    }


def test_merge_prefixes_suite_paths_and_counts_install_checks(tmp_path: Path):
    checks = Checks("install")
    checks.record("udev", "udev rule", False, "missing")
    report = write_target_report(tmp_path, FakeTarget(), checks, auto_qa_report=auto_qa_report("pass"),
                               problem=None)
    assert report["counts"] == {"pass": 1, "fail": 1, "skip": 0, "error": 0}
    assert report["result"] == "fail"  # a failed install check fails the target even when --auto-qa passed
    assert report["tests"][1]["artifacts"][0]["path"] == "auto-qa/plots/x.png"
    assert report["screenshots"][0]["path"] == "auto-qa/screenshots/s.png"
    assert json.loads((tmp_path / "report.json").read_text()) == report


def test_auto_qa_problem_is_an_error_test(tmp_path: Path):
    report = write_target_report(tmp_path, FakeTarget(), Checks("install"), auto_qa_report=None,
                               problem="--auto-qa exited 1 without writing report.json")
    assert report["counts"]["error"] == 1 and report["result"] == "fail"


def test_release_fingerprint_follows_assets():
    a = Asset("x.deb", 1, "aa", "t", "u")
    r1 = Release("o/r", "t", "n", "c" * 40, "p", {"x.deb": a})
    r2 = Release("o/r", "t", "n", "c" * 40, "p", {"x.deb": Asset("x.deb", 1, "bb", "t", "u")})
    assert r1.fingerprint == Release("o/r", "t", "n", "c" * 40, "p", {"x.deb": a}).fingerprint
    assert r1.fingerprint != r2.fingerprint
