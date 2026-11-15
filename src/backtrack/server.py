"""HTTP: the signed agent API (every agent), and the hub's UI and public API (HUB=true)."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from backtrack import __version__, ui
from backtrack.hub import AgentUnreachable, Hub
from backtrack.signing import Verifier
from backtrack.stacks import NAME_RE

log = logging.getLogger("backtrack")
SESSION_TTL = 12 * 3600
MAX_BODY = 64 * 1024
STATIC = {"tokens.css", "plain.css", "bundle.css", "app.css", "app.js", "mark.svg"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _stack_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise ApiError(400, "invalid stack name")
    return name


def local_agent_call(service, method: str, path: str, body: dict | None) -> dict:
    """The agent API, callable in-process (the hub uses this for its own machine)."""
    body = body or {}
    parts = [p for p in path.split("?")[0].split("/") if p]
    if parts[:1] != ["v1"]:
        raise ApiError(404, "not found")
    parts = parts[1:]
    if method == "GET" and parts == ["status"]:
        return service.status()
    if method == "GET" and parts == ["stacks"]:
        return {"stacks": service.presence()["stacks"]}
    if method == "GET" and parts == ["jobs"]:
        return {"jobs": [j.to_dict() for j in service.runner.recent(50)]}
    if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
        job = service.runner.get(parts[1])
        if not job:
            raise ApiError(404, "no such job")
        return job.to_dict(with_log=True)
    if parts == ["settings"]:
        if method == "GET":
            return {"settings": service.cfg.settings()}
        if method == "PUT":
            errors = service.cfg.update_settings({k: str(v) for k, v in body.items()})
            if errors:
                raise ApiError(400, "; ".join(errors))
            return {"settings": service.cfg.settings()}
    if method == "POST" and len(parts) == 3 and parts[0] == "stacks":
        stack = _stack_name(parts[1])
        if parts[2] == "backup":
            trigger = body.get("trigger", "manual")
            if trigger not in ("manual", "pre-migrate"):
                raise ApiError(400, "trigger must be manual or pre-migrate")
            if not service.agent.find(stack):
                raise ApiError(404, f"no stack {stack} on {service.cfg.machine}")
            return {"job": service.submit("backup", stack, trigger=trigger).to_dict()}
        if parts[2] == "restore":
            snapshot = str(body.get("snapshot") or "latest")
            if not re.fullmatch(r"latest|[0-9a-f]{8,64}", snapshot):
                raise ApiError(400, "snapshot must be 'latest' or a snapshot id")
            return {"job": service.submit("restore", stack, snapshot=snapshot).to_dict()}
    raise ApiError(404, "not found")


class Sessions:
    def __init__(self):
        self._items: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._failures: list[float] = []

    def create(self) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._lock:
            self._items[token] = (time.time() + SESSION_TTL, csrf)
        return token, csrf

    def get(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            item = self._items.get(token)
            if not item or item[0] < time.time():
                self._items.pop(token, None)
                return None
            return item[1]

    def drop(self, token: str | None) -> None:
        with self._lock:
            self._items.pop(token or "", None)

    def throttled(self) -> bool:
        now = time.time()
        with self._lock:
            self._failures = [t for t in self._failures if now - t < 300]
            return len(self._failures) >= 10

    def failed(self) -> None:
        with self._lock:
            self._failures.append(time.time())


def make_handler(service, hub: Hub | None):
    cfg = service.cfg
    verifier = Verifier(cfg.get("CLUSTER_KEY"))
    sessions = Sessions()

    class Handler(BaseHTTPRequestHandler):
        server_version = "backtrack"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        # -- plumbing ---------------------------------------------------------------------------------------
        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ApiError(413, "request too large")
            return self.rfile.read(n) if n else b""

        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _html(self, html: str, status: int = 200, headers: dict | None = None) -> None:
            self._send(status, html.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-store", **(headers or {})})

        def _redirect(self, location: str, headers: dict | None = None) -> None:
            self._send(303, b"", "text/plain", {"Location": location, **(headers or {})})

        def _cookie(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c["bt_session"].value if "bt_session" in c else None

        def _form(self, raw: bytes) -> dict[str, str]:
            return {k: v[0] for k, v in parse_qs(raw.decode(errors="replace")).items()}

        def _token_scope(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            given = auth[7:].strip().encode()
            write, read = cfg.get("WRITE_TOKEN").encode(), cfg.get("READ_TOKEN").encode()
            if write and hmac.compare_digest(given, write):
                return "write"
            if read and hmac.compare_digest(given, read):
                return "read"
            return None

        # -- dispatch ---------------------------------------------------------------------------------------
        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            path = url.path
            try:
                body = self._body() if method in ("POST", "PUT") else b""
                if path == "/healthz":
                    return self._json(200, {"ok": True, "machine": cfg.machine, "version": __version__})
                if path.startswith("/v1/"):
                    return self._agent_api(method, self.path, body)
                if not hub:
                    raise ApiError(404, "not found")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path.startswith("/api/v1/"):
                    return self._hub_api(method, path[len("/api/v1"):], body)
                return self._ui(method, path, parse_qs(url.query), body)
            except ApiError as e:
                if path.startswith(("/v1/", "/api/")) or not hub:
                    return self._json(e.status, {"error": str(e)})
                return self._html(f"<!doctype html><title>{e.status}</title><p>{ui.e(str(e))}</p>", e.status)
            except Exception as e:  # noqa: BLE001
                log.exception("request failed")
                return self._json(500, {"error": f"internal error: {type(e).__name__}"})

        # -- agent API (signed by the hub) ------------------------------------------------------------------
        def _agent_api(self, method: str, raw_path: str, body: bytes) -> None:
            if not verifier.verify(method, raw_path, self.headers, body):
                raise ApiError(401, "bad or missing signature")
            data = json.loads(body) if body else None
            status = 202 if method == "POST" else 200
            self._json(status, local_agent_call(service, method, raw_path, data))

        # -- hub public API ---------------------------------------------------------------------------------
        def _hub_api(self, method: str, path: str, body: bytes) -> None:
            scope = self._token_scope()
            csrf = sessions.get(self._cookie())
            if not scope and csrf:
                # A signed-in browser may call the API; writes must carry the CSRF token.
                scope = "write" if method == "GET" or self.headers.get("X-CSRF-Token") == csrf else "read"
            if not scope:
                raise ApiError(401, "missing or wrong bearer token")
            parts = [p for p in path.split("/") if p]
            if method == "GET":
                if parts == ["status"]:
                    agents = hub.agents()
                    lease = hub.share.lease()
                    return self._json(200, {
                        "hub": cfg.machine, "version": __version__,
                        "agents": {"total": len(agents), "online": sum(1 for a in agents if a.get("online"))},
                        "maintenance": lease.to_dict() if lease else None,
                        "warnings": [{"level": w[0], "title": w[1], "detail": w[2]} for w in hub.warnings()],
                        "stacks": {"total": sum(len(a.get("stacks", [])) for a in agents),
                                   "failed": sum(1 for a in agents for s in a.get("stacks", [])
                                                 if (s.get("last") or {}).get("state") == "failed")}})
                if parts == ["agents"]:
                    return self._json(200, {"agents": [_public_agent(a) for a in hub.agents()]})
                if parts == ["stacks"]:
                    return self._json(200, {"stacks": [{**s, "machine": a["machine"], "agent_online": a.get("online")}
                                                       for a in hub.agents() for s in a.get("stacks", [])]})
                if len(parts) == 3 and parts[0] == "stacks" and parts[2] == "snapshots":
                    return self._json(200, {"snapshots": hub.snapshots(_stack_name(parts[1]))})
                if len(parts) == 3 and parts[0] == "jobs":
                    try:
                        return self._json(200, hub.call(parts[1], "GET", f"/v1/jobs/{parts[2]}"))
                    except AgentUnreachable as e:
                        raise ApiError(502, str(e)) from e
            if method == "POST" and len(parts) == 3 and parts[0] == "stacks":
                if scope != "write":
                    raise ApiError(403, "this token is read-only")
                stack = _stack_name(parts[1])
                data = json.loads(body or b"{}")
                try:
                    if parts[2] == "backup":
                        machine, res = hub.backup(stack, data.get("trigger", "manual"))
                        return self._json(202, {"machine": machine, **res})
                    if parts[2] == "restore":
                        if data.get("confirm") != stack:
                            raise ApiError(400, f'set "confirm" to "{stack}" to restore')
                        machine, res = hub.restore(stack, str(data.get("snapshot") or "latest"))
                        return self._json(202, {"machine": machine, **res})
                except AgentUnreachable as e:
                    raise ApiError(502, str(e)) from e
            raise ApiError(404, "not found")

        # -- static -----------------------------------------------------------------------------------------
        def _static(self, name: str) -> None:
            base = resources.files("backtrack") / "static"
            if name in STATIC:
                f = base / name
            elif re.fullmatch(r"fonts/[a-z0-9-]+\.woff2", name):
                f = base / name
            else:
                raise ApiError(404, "not found")
            try:
                data = f.read_bytes()
            except FileNotFoundError as e:
                raise ApiError(404, "not found") from e
            ctype = mimetypes.guess_type(name)[0] or ("font/woff2" if name.endswith(".woff2") else "text/plain")
            self._send(200, data, ctype, {"Cache-Control": "public, max-age=86400"})

        # -- UI ---------------------------------------------------------------------------------------------
        def _ui(self, method: str, path: str, query: dict, body: bytes) -> None:
            theme = cfg.get("THEME")
            if path == "/login":
                if method == "POST":
                    if sessions.throttled():
                        return self._html(ui.login(theme, "Too many attempts. Wait a few minutes."), 429)
                    given = self._form(body).get("password", "").encode()
                    if hmac.compare_digest(given, cfg.get("UI_PASSWORD").encode()):
                        token, _ = sessions.create()
                        return self._redirect("/", {"Set-Cookie": f"bt_session={token}; Path=/; HttpOnly; Secure; "
                                                                  f"SameSite=Strict; Max-Age={SESSION_TTL}"})
                    sessions.failed()
                    return self._html(ui.login(theme, "That password isn't right."), 401)
                return self._html(ui.login(theme))
            token = self._cookie()
            csrf = sessions.get(token)
            if not csrf:
                return self._redirect("/login")
            form = {}
            if method == "POST":
                form = self._form(body)
                if not hmac.compare_digest(form.get("csrf", "").encode(), csrf.encode()):
                    raise ApiError(403, "the form expired; reload the page")
            if path == "/logout" and method == "POST":
                sessions.drop(token)
                return self._redirect("/login", {"Set-Cookie": "bt_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})

            done = (query.get("done") or [None])[0]
            detail = (query.get("detail") or [""])[0][:300]
            t = ui.toast(done, detail)
            alerts = len(hub.warnings())

            def render(title, current, body_html):
                return self._html(ui.page(title, current, body_html, theme, csrf, t, alerts))

            parts = [p for p in path.split("/") if p]
            if method == "GET":
                if not parts:
                    count, _ = hub.snapshot_count()
                    lease = hub.share.lease()
                    return render("Stacks", "stacks", ui.stacks_page(hub.agents(), count, lease.to_dict() if lease else None,
                                                                     hub.warnings(), csrf))
                if len(parts) == 2 and parts[0] == "stacks":
                    found = hub.find_stack(_stack_name(parts[1]))
                    if not found:
                        raise ApiError(404, "no such stack")
                    try:
                        snaps = hub.snapshots(parts[1])
                    except Exception:  # noqa: BLE001
                        snaps = []
                    return render(parts[1], "stacks", ui.stack_page(found[0], found[1], snaps, csrf))
                if parts == ["agents"]:
                    lease = hub.share.lease()
                    return render("Agents", "agents", ui.agents_page(hub.agents(), lease.to_dict() if lease else None))
                if len(parts) == 2 and parts[0] == "agents":
                    agent = hub.agent(parts[1])
                    if not agent:
                        raise ApiError(404, "no such agent")
                    try:
                        settings = hub.call(parts[1], "GET", "/v1/settings")["settings"]
                    except AgentUnreachable:
                        settings = None
                    lease = hub.share.lease()
                    return render(parts[1], "agents", ui.agent_page(agent, settings, lease.to_dict() if lease else None,
                                                                    cfg.machine, csrf, detail if done == "error" else ""))
                if parts == ["jobs"]:
                    jobs, unreachable = hub.jobs()
                    return render("Jobs", "jobs", ui.jobs_page(jobs, unreachable))
                if len(parts) == 3 and parts[0] == "jobs":
                    try:
                        job = hub.call(parts[1], "GET", f"/v1/jobs/{parts[2]}")
                    except AgentUnreachable as e:
                        raise ApiError(502, str(e)) from e
                    return render(job.get("kind", "Job"), "jobs", ui.job_page(parts[1], job))
                if parts == ["repository"]:
                    count, stacks = hub.snapshot_count()
                    lease = hub.share.lease()
                    return render("Repository", "repository", ui.repository_page(
                        cfg.get("RESTIC_REPOSITORY"), count, stacks, lease.to_dict() if lease else None,
                        hub.agents(), csrf))
            if method == "POST":
                try:
                    if len(parts) == 3 and parts[0] == "stacks" and parts[2] == "backup":
                        stack = _stack_name(parts[1])
                        trigger = form.get("trigger", "manual")
                        machine, res = hub.backup(stack, trigger)
                        return self._redirect(f"/jobs/{machine}/{res['job']['id']}?done="
                                              f"{'migrate' if trigger == 'pre-migrate' else 'backup'}")
                    if len(parts) == 3 and parts[0] == "stacks" and parts[2] == "restore":
                        stack = _stack_name(parts[1])
                        if form.get("confirm") != stack:
                            return self._redirect(f"/stacks/{stack}?done=error&detail=Type+the+stack+name+to+confirm.")
                        machine, res = hub.restore(stack, form.get("snapshot") or "latest")
                        return self._redirect(f"/jobs/{machine}/{res['job']['id']}?done=restore")
                    if len(parts) == 3 and parts[0] == "agents" and parts[2] == "settings":
                        changes = {k: v for k, v in form.items() if k != "csrf"}
                        try:
                            hub.call(parts[1], "PUT", "/v1/settings", changes)
                        except (AgentUnreachable, ApiError) as e:
                            return self._redirect(f"/agents/{parts[1]}?done=error&detail={_q(str(e))}")
                        return self._redirect(f"/agents/{parts[1]}?done=saved")
                    if parts == ["repository", "lease"]:
                        hub.assign_lease(form.get("machine", ""))
                        back = self.headers.get("Referer", "")
                        target = "/repository" if "/agents/" not in back else f"/agents/{form.get('machine', '')}"
                        return self._redirect(f"{target}?done=lease")
                except AgentUnreachable as e:
                    return self._redirect(f"/?done=error&detail={_q(str(e))}")
            raise ApiError(404, "not found")

    return Handler


def _q(s: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(s[:300])


def _public_agent(a: dict) -> dict:
    return {k: a.get(k) for k in ("machine", "online", "version", "hub", "job", "last_nightly", "maintenance",
                                  "next_runs", "seen_at", "docker_error")} | {"stacks": len(a.get("stacks", []))}


def serve(service, listen: str) -> ThreadingHTTPServer:
    host, _, port = listen.rpartition(":")
    hub = Hub(service, service.cfg.get("CLUSTER_KEY")) if service.cfg.hub else None
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port)), make_handler(service, hub))
    httpd.daemon_threads = True
    return httpd
