import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import pytest

from backtrack.server import serve
from backtrack.service import Service
from backtrack.signing import sign
from backtrack.stacks import L_MODE
from tests.conftest import Lab

KEY = "cluster-key-for-tests"
HUB_ENV = {"HUB": "true", "UI_PASSWORD": "correct horse", "READ_TOKEN": "read-tok", "WRITE_TOKEN": "write-tok",
           "CLUSTER_KEY": KEY, "LISTEN": "127.0.0.1:0"}


@pytest.fixture
def hub(tmp_path, repo):
    lab = Lab(tmp_path, repo, machine="alpha", extra_env=HUB_ENV)
    lab.stack_dir("app", {"f": "1"})
    lab.docker.add("app", "web", {L_MODE: "stop"})
    service = Service(lab.cfg, lab.agent)
    service.runner.start()
    httpd = serve(service, "127.0.0.1:0")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    lab.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    lab.service = service
    yield lab
    httpd.shutdown()
    service.runner.stop(wait=5)


def req(lab, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(lab.base + path, data=data, method=method,
                               headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_job(lab, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = lab.service.runner.get(job_id)
        if job and job.state in ("ok", "failed"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_healthz_needs_no_auth(hub):
    assert req(hub, "GET", "/healthz") == (200, {"ok": True, "machine": "alpha", "version": "0.1.1"})


def test_agent_api_requires_signature(hub):
    assert req(hub, "GET", "/v1/status")[0] == 401
    status, body = req(hub, "GET", "/v1/status", headers=sign(KEY, "GET", "/v1/status"))
    assert status == 200 and body["machine"] == "alpha"
    assert req(hub, "GET", "/v1/status", headers=sign("wrong", "GET", "/v1/status"))[0] == 401


def test_hub_api_token_scopes(hub):
    assert req(hub, "GET", "/api/v1/status")[0] == 401
    assert req(hub, "GET", "/api/v1/status", headers={"Authorization": "Bearer correct horse"})[0] == 401
    status, body = req(hub, "GET", "/api/v1/stacks", headers={"Authorization": "Bearer read-tok"})
    assert status == 200 and body["stacks"][0]["name"] == "app" and body["stacks"][0]["machine"] == "alpha"
    s, _ = req(hub, "POST", "/api/v1/stacks/app/backup", {"trigger": "manual"},
               headers={"Authorization": "Bearer read-tok"})
    assert s == 403


def test_backup_and_restore_through_hub_api(hub):
    auth = {"Authorization": "Bearer write-tok"}
    s, body = req(hub, "POST", "/api/v1/stacks/app/backup", {"trigger": "manual"}, headers=auth)
    assert s == 202 and body["machine"] == "alpha"
    assert wait_job(hub, body["job"]["id"]).state == "ok"
    s, snaps = req(hub, "GET", "/api/v1/stacks/app/snapshots", headers=auth)
    assert s == 200 and len(snaps["snapshots"]) == 1

    s, err = req(hub, "POST", "/api/v1/stacks/app/restore", {"snapshot": "latest"}, headers=auth)
    assert s == 400 and "confirm" in err["error"]
    s, body = req(hub, "POST", "/api/v1/stacks/app/restore", {"snapshot": "latest", "confirm": "app"}, headers=auth)
    assert s == 202
    assert wait_job(hub, body["job"]["id"]).state == "ok"
    assert (hub.appdata / "app.pre-restore").is_dir()

    s, job = req(hub, "GET", f"/api/v1/jobs/alpha/{body['job']['id']}", headers=auth)
    assert s == 200 and job["state"] == "ok" and any("files restored" in line for line in job["log"])


def test_status_reports_warnings(hub):
    s, body = req(hub, "GET", "/api/v1/status", headers={"Authorization": "Bearer read-tok"})
    assert s == 200 and body["agents"] == {"total": 1, "online": 1}
    assert any(w["title"] == "No maintenance agent" for w in body["warnings"])


def test_ui_login_csrf_and_pages(hub):
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(path):
        with opener.open(hub.base + path, timeout=10) as r:
            return r.status, r.geturl(), r.read().decode()

    def post(path, form):
        data = urllib.parse.urlencode(form).encode()
        try:
            with opener.open(urllib.request.Request(hub.base + path, data=data, method="POST"), timeout=10) as r:
                return r.status, r.geturl(), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, path, e.read().decode()


    _, url, html = get("/")
    assert url.endswith("/login") and "Sign in to Backtrack" in html
    assert post("/login", {"password": "nope"})[0] == 401
    # urllib drops Secure cookies on http, so set the session cookie by hand from the response
    r = urllib.request.Request(hub.base + "/login", data=b"password=correct+horse", method="POST")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(NoRedirect).open(r, timeout=10)
    except urllib.error.HTTPError as e:
        cookie = e.headers["Set-Cookie"].split(";")[0]
    opener.addheaders = [("Cookie", cookie)]

    status, _, html = get("/")
    assert status == 200 and "app" in html and "/static/style.css" in html
    csrf = html.split('name="csrf" value="')[1].split('"')[0]
    for path in ("/stacks/app", "/agents", "/agents/alpha", "/jobs", "/repository"):
        assert get(path)[0] == 200, path
    assert post("/stacks/app/backup", {"trigger": "manual"})[0] == 403  # no CSRF token
    status, url, html = post("/stacks/app/backup", {"trigger": "manual", "csrf": csrf})
    assert status == 200 and "/jobs/alpha/" in url and "Backup queued" in html
    status, url, html = post("/agents/alpha/settings", {"KEEP": "daily=9", "csrf": csrf})
    assert "done=saved" in url and hub.cfg.get("KEEP") == "daily=9"
    status, url, _ = post("/repository/lease", {"machine": "alpha", "csrf": csrf})
    assert "done=lease" in url and hub.share.lease().machine == "alpha"
    assert hub.share.lease().assigned_by == "hub:alpha"


def test_static_files_only_from_allowlist(hub):
    with urllib.request.urlopen(hub.base + "/static/style.css", timeout=10) as r:
        assert r.status == 200 and b"--bg" in r.read(2000)
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(hub.base + "/static/../server.py", timeout=10)


def test_prerestore_through_hub_api(hub):
    auth = {"Authorization": "Bearer write-tok"}
    s, body = req(hub, "POST", "/api/v1/stacks/app/backup", {"trigger": "manual"}, headers=auth)
    wait_job(hub, body["job"]["id"])
    s, body = req(hub, "POST", "/api/v1/stacks/app/restore", {"snapshot": "latest", "confirm": "app"}, headers=auth)
    wait_job(hub, body["job"]["id"])
    aside = str(hub.appdata / "app.pre-restore")

    s, body = req(hub, "GET", "/api/v1/prerestore", headers={"Authorization": "Bearer read-tok"})
    assert s == 200 and [(f["path"], f["machine"], f["state"]) for f in body["folders"]] == [(aside, "alpha", "settling")]
    s, _ = req(hub, "POST", "/api/v1/stacks/app/prerestore/remove", {"path": aside, "confirm": "app"},
               headers={"Authorization": "Bearer read-tok"})
    assert s == 403
    s, err = req(hub, "POST", "/api/v1/stacks/app/prerestore/remove", {"path": aside}, headers=auth)
    assert s == 400 and "confirm" in err["error"]
    s, _ = req(hub, "POST", "/api/v1/stacks/app/prerestore/remove", {"path": "/etc", "confirm": "app"}, headers=auth)
    assert s == 404

    # Too early: the agent checks again and refuses.
    s, body = req(hub, "POST", "/api/v1/stacks/app/prerestore/remove", {"path": aside, "confirm": "app"}, headers=auth)
    assert s == 202
    job = wait_job(hub, body["job"]["id"])
    assert job.state == "failed" and "isn't healthy" in job.error

    hub.agent.clock = lambda: time.time() + 3600
    s, body = req(hub, "GET", "/api/v1/status", headers=auth)
    assert any("can be removed" in w["title"] for w in body["warnings"])
    s, body = req(hub, "POST", "/api/v1/stacks/app/prerestore/remove", {"path": aside, "confirm": "app"}, headers=auth)
    assert wait_job(hub, body["job"]["id"]).state == "ok"
    assert not (hub.appdata / "app.pre-restore").exists()
    assert req(hub, "GET", "/api/v1/prerestore", headers=auth)[1] == {"folders": []}


def test_stack_page_lists_set_aside_folders():
    from backtrack import ui
    folder = {"path": "/opt/appdata/app.pre-restore", "stack": "app", "machine": "alpha", "created": time.time(),
              "size": 2048, "snapshot": "aaaa1111", "safety_snapshot": "bbbb2222", "state": "safe",
              "reason": "healthy", "removable": True}
    stack = {"name": "app", "mode": "stop", "path": "/opt/appdata/app", "containers": []}
    html = ui.stack_page(stack, {"machine": "alpha"}, [], "tok", [folder])
    assert "Set-aside folders" in html and 'action="/stacks/app/prerestore/remove"' in html
    assert 'value="/opt/appdata/app.pre-restore"' in html and "Safe to remove" in html
    html = ui.stack_page(stack, {"machine": "alpha"}, [], "tok", [{**folder, "state": "settling", "removable": False}])
    assert "prerestore/remove" not in html and "Settling" in html
