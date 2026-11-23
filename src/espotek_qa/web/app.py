"""Password-protected results site (qa-results.espotek.com), served by waitress
on localhost behind a Cloudflare tunnel. Runs as the unprivileged `qaweb`
account and only ever reads the results directory.

Humans log in with the password (session cookie). Agents send the same
password with HTTP Basic auth (any username) and use the /api/ routes.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, send_from_directory,
                   session, url_for)
from werkzeug.security import check_password_hash
from werkzeug.utils import safe_join

MAX_FAILS = 8
FAIL_WINDOW_S = 900


class LoginThrottle:
    """Refuse logins from an address after MAX_FAILS failures in FAIL_WINDOW_S."""

    def __init__(self):
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, addr: str) -> bool:
        with self._lock:
            q = self._fails[addr]
            while q and q[0] < time.monotonic() - FAIL_WINDOW_S:
                q.popleft()
            return len(q) >= MAX_FAILS

    def fail(self, addr: str) -> None:
        with self._lock:
            self._fails[addr].append(time.monotonic())


def create_app(results_dir: Path, password_hash: str, secret_key: str) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=30 * 24 * 3600,
    )
    throttle = LoginThrottle()

    def client_addr() -> str:
        # cloudflared is the only client of this localhost socket; it passes the visitor's address here.
        return request.headers.get("CF-Connecting-IP", request.remote_addr or "unknown")

    def password_ok(pw: str) -> bool:
        return check_password_hash(password_hash, pw)

    @app.before_request
    def require_auth():
        if request.endpoint in ("login", "healthz", "static"):
            return None
        if session.get("authed"):
            return None
        auth = request.authorization
        if auth is not None and auth.type == "basic":
            addr = client_addr()
            if throttle.blocked(addr):
                return Response("too many failed logins; try later\n", 429)
            if auth.password and password_ok(auth.password):
                return None
            throttle.fail(addr)
        # API clients and anyone who sent (wrong) Basic credentials get a 401; browsers go to the login page.
        if request.path.startswith("/api/") or auth is not None:
            return Response("authentication required\n", 401,
                            {"WWW-Authenticate": 'Basic realm="Labrador QA results"'})
        return redirect(url_for("login", next=request.full_path))

    @app.after_request
    def headers(resp: Response) -> Response:
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        nxt = request.values.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if request.method == "POST":
            addr = client_addr()
            if throttle.blocked(addr):
                error = "Too many failed attempts from your address. Try again in a few minutes."
            elif password_ok(request.form["password"]):
                session.clear()
                session["authed"] = True
                session.permanent = True
                return redirect(nxt)
            else:
                throttle.fail(addr)
                error = "That password isn't right."
        return render_template("login.html", error=error, next=nxt)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/healthz")
    def healthz():
        return "ok\n"

    # --- data -----------------------------------------------------------------

    def load(*parts: str) -> dict:
        path = safe_join(str(results_dir), *parts)
        if path is None or not os.path.isfile(path):
            abort(404)
        with open(path) as f:
            return json.load(f)

    def index() -> dict:
        if not (results_dir / "index.json").exists():
            return {"runs": []}
        return load("index.json")

    def target_report(run_id: str, target: str) -> tuple[dict, dict]:
        report = load(run_id, target, "report.json")
        screens_path = safe_join(str(results_dir), run_id, target, "vm-screens", "index.json")
        screens = {"screens": []}
        if screens_path is not None and os.path.isfile(screens_path):
            with open(screens_path) as f:
                screens = json.load(f)
        return report, screens

    # --- pages ----------------------------------------------------------------

    @app.get("/")
    def runs():
        return render_template("runs.html", runs=index()["runs"])

    @app.get("/runs/<run_id>")
    def run(run_id: str):
        return render_template("run.html", run=load(run_id, "run.json"))

    @app.get("/runs/<run_id>/<target>")
    def target(run_id: str, target: str):
        summary = load(run_id, "run.json")
        entry = next((t for t in summary["targets"] if t["id"] == target), None)
        if entry is None:
            abort(404)
        if entry["status"] != "done":
            return render_template("target_pending.html", run=summary, entry=entry)
        report, screens = target_report(run_id, target)
        order = {"error": 0, "fail": 1, "skip": 2, "pass": 3}
        tests = sorted(report["tests"], key=lambda t: order[t["status"]])
        return render_template("target.html", run=summary, entry=entry, report=report, tests=tests,
                               screens=screens["screens"])

    @app.get("/files/<path:path>")
    def files(path: str):
        full = safe_join(str(results_dir), path)
        if full is None or not os.path.isfile(full):
            abort(404)
        text_like = path.endswith((".log", ".txt", ".csv", ".json", ".argv"))
        return send_from_directory(results_dir, path, mimetype="text/plain; charset=utf-8" if text_like else None,
                                   max_age=3600)

    # --- API for agents ---------------------------------------------------------

    @app.get("/api/runs")
    def api_runs():
        return jsonify(index())

    @app.get("/api/runs/latest")
    def api_latest():
        runs = index()["runs"]
        if not runs:
            abort(404)
        return jsonify(load(runs[0]["run_id"], "run.json"))

    @app.get("/api/runs/<run_id>")
    def api_run(run_id: str):
        return jsonify(load(run_id, "run.json"))

    @app.get("/api/runs/<run_id>/<target>")
    def api_target(run_id: str, target: str):
        report, screens = target_report(run_id, target)
        return jsonify(report | {"vm_screens": screens["screens"]})

    @app.get("/agents")
    def agents():
        return Response(render_template("agents.txt", host=request.host), mimetype="text/plain; charset=utf-8")

    return app


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, taken literally (password hashes contain `$`, so never source this as shell)."""
    env = {}
    for line in path.read_text().splitlines():
        if line.strip():
            key, value = line.split("=", 1)
            env[key] = value
    return env


def serve(env_file: Path) -> None:
    from waitress import serve as waitress_serve

    env = read_env_file(env_file)
    results = Path(env["QA_RESULTS_DIR"])
    app = create_app(results, env["QA_WEB_PASSWORD_HASH"], env["QA_WEB_SECRET_KEY"])
    port = int(env["QA_WEB_PORT"])
    print(f"serving {results} on 127.0.0.1:{port}", flush=True)
    waitress_serve(app, host="127.0.0.1", port=port, threads=8, ident="labrador-qa")


def new_secret() -> str:
    return secrets.token_urlsafe(48)
