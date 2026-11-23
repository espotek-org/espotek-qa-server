import base64
import json
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from espotek_qa.web.app import create_app, read_env_file


def basic(user: str, pw: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def results(tmp_path: Path) -> Path:
    run = tmp_path / "r1"
    (run / "linux-deb").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({
        "run_id": "r1", "status": "done", "result": "pass", "started_at": "2026-11-23T08:00:00+00:00",
        "finished_at": "2026-11-23T09:00:00+00:00", 
        "release": {"tag": "unified-continuous", "name": "x", "commit": "c" * 40, "published_at": "2026-10-22",
                    "assets": {}},
        "targets": [{"id": "linux-deb", "asset": "a.deb", "status": "done", "result": "pass",
                     "counts": {"pass": 1, "fail": 0, "skip": 0, "error": 0}, "message": ""}],
    }))
    (run / "linux-deb" / "report.json").write_text(json.dumps({
        "result": "pass", "counts": {"pass": 1, "fail": 0, "skip": 0, "error": 0}, "asset": "a.deb", "auto_qa": None,
        "tests": [{"id": "t", "group": "g", "title": "T", "status": "pass", "duration_s": 1.0, "message": "",
                   "measurements": [], "artifacts": []}], "screenshots": [],
    }))
    (tmp_path / "index.json").write_text(json.dumps({"runs": [{
        "run_id": "r1", "status": "done", "result": "pass", "started_at": "2026-11-23T08:00:00+00:00",
        "finished_at": "x", "commit": "c" * 40, "tag": "unified-continuous",
        "targets": [{"id": "linux-deb", "result": "pass", "counts": None}]}]}))
    return tmp_path


@pytest.fixture
def client(results: Path):
    return create_app(results, generate_password_hash("pw"), "key").test_client()


def test_browser_without_session_is_sent_to_login(client):
    r = client.get("/runs/r1")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")


def test_api_without_credentials_is_401(client):
    assert client.get("/api/runs").status_code == 401


def test_basic_auth(client):
    assert client.get("/api/runs", headers=basic("qa", "pw")).status_code == 200
    assert client.get("/api/runs", headers=basic("qa", "nope")).status_code == 401


def test_login_sets_session_and_honours_safe_next_only(client):
    r = client.post("/login", data={"password": "pw", "next": "//evil.example/"})
    assert r.headers["Location"] == "/"
    assert client.get("/runs/r1/linux-deb").status_code == 200


def test_repeated_failures_are_throttled(client):
    for _ in range(8):
        client.get("/api/runs", headers=basic("qa", "nope"))
    assert client.get("/api/runs", headers=basic("qa", "pw")).status_code == 429


def test_files_cannot_escape_the_results_dir(client):
    h = basic("qa", "pw")
    assert client.get("/files/r1/run.json", headers=h).status_code == 200
    assert client.get("/files/../../../etc/passwd", headers=h).status_code == 404


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").status_code == 200


def test_env_file_values_are_literal(tmp_path: Path):
    # werkzeug scrypt hashes contain `$`; sourcing them as shell would mangle them.
    f = tmp_path / "web.env"
    f.write_text("QA_WEB_PASSWORD_HASH=scrypt:32768:8:1$salt$hash\nQA_WEB_PORT=8890\n")
    assert read_env_file(f)["QA_WEB_PASSWORD_HASH"] == "scrypt:32768:8:1$salt$hash"
