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
    assert req(hub, "GET", "/healthz") == (200, {"ok": True, "machine": "alpha", "version": "0.3.0"})


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
    assert "Back up all" in html and "Stops while backed up" in html and "Back up 1 stack<" in html
    assert 'name="machine" value="alpha"' in get("/agents/alpha")[2]  # the agent page's own button
    assert post("/stacks/app/backup", {"trigger": "manual"})[0] == 403  # no CSRF token
    status, url, html = post("/stacks/app/backup", {"trigger": "manual", "csrf": csrf})
    assert status == 200 and "/jobs/alpha/" in url and "Backup queued" in html
    status, url, html = post("/backup-all", {"csrf": csrf})
    assert status == 200 and "/jobs/alpha/" in url and "Back up all queued" in html
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


def test_static_urls_carry_a_content_hash(hub):
    from backtrack import ui
    html = ui.login()
    for name in ("mark.svg", "style.css"):
        url = ui.static(name)
        assert url.startswith(f"/static/{name}?v=") and len(url.split("=")[1]) == 10 and url in html
        with urllib.request.urlopen(hub.base + url, timeout=10) as r:  # the server ignores the query
            assert r.status == 200


def test_job_list_with_filters(hub):
    auth = {"Authorization": "Bearer write-tok"}
    hub.stack_dir("db", {"f": "2"})
    hub.docker.add("db", "postgres", {L_MODE: "stop"})
    ids = []
    for stack in ("app", "db", "app"):
        s, body = req(hub, "POST", f"/api/v1/stacks/{stack}/backup", {"trigger": "manual"}, headers=auth)
        ids.append(body["job"]["id"])
        wait_job(hub, ids[-1])
    s, body = req(hub, "POST", "/api/v1/stacks/app/restore", {"snapshot": "deadbeef", "confirm": "app"}, headers=auth)
    ids.append(body["job"]["id"])
    assert wait_job(hub, ids[-1]).state == "failed"

    read = {"Authorization": "Bearer read-tok"}
    s, body = req(hub, "GET", "/api/v1/jobs", headers=read)
    assert s == 200 and body["unreachable"] == []
    assert [j["id"] for j in body["jobs"]] == ids[::-1]
    first = body["jobs"][-1]
    assert {k: first[k] for k in ("machine", "kind", "stack", "trigger", "state", "error")} == {
        "machine": "alpha", "kind": "backup", "stack": "app", "trigger": "manual", "state": "ok", "error": None}
    assert first["started"] and first["finished"] >= first["started"] and "log" not in first
    assert body["jobs"][0]["kind"] == "restore" and body["jobs"][0]["error"]

    assert [j["id"] for j in req(hub, "GET", "/api/v1/jobs?limit=2", headers=read)[1]["jobs"]] == ids[:1:-1]
    assert [j["id"] for j in req(hub, "GET", "/api/v1/jobs?stack=db", headers=read)[1]["jobs"]] == [ids[1]]
    assert [j["id"] for j in req(hub, "GET", "/api/v1/jobs?state=failed", headers=read)[1]["jobs"]] == [ids[3]]
    assert [j["id"] for j in req(hub, "GET", "/api/v1/jobs?stack=app&state=ok&limit=1",
                                 headers=read)[1]["jobs"]] == [ids[2]]
    assert len(req(hub, "GET", "/api/v1/jobs?machine=alpha", headers=read)[1]["jobs"]) == 4
    assert req(hub, "GET", "/api/v1/jobs?machine=nope", headers=read)[0] == 404
    assert req(hub, "GET", "/api/v1/jobs?state=weird", headers=read)[0] == 400
    assert req(hub, "GET", "/api/v1/jobs?limit=x", headers=read)[0] == 400
    assert req(hub, "GET", "/api/v1/jobs?stack=../x", headers=read)[0] == 400
    assert req(hub, "GET", "/api/v1/jobs")[0] == 401


def test_stack_detail(hub):
    read = {"Authorization": "Bearer read-tok"}
    s, body = req(hub, "GET", "/api/v1/stacks/app", headers=read)
    assert s == 200 and body["name"] == "app" and body["machine"] == "alpha" and body["agent_online"] is True
    assert body["mode"] == "stop" and body["also_on"] == [] and body["prerestore"] == []
    assert req(hub, "GET", "/api/v1/stacks/nope", headers=read)[0] == 404
    assert req(hub, "GET", "/api/v1/stacks/app")[0] == 401


def test_backup_all_through_hub_api(hub):
    auth = {"Authorization": "Bearer write-tok"}
    assert req(hub, "POST", "/api/v1/backup-all", {"confirm": "all"},
               headers={"Authorization": "Bearer read-tok"})[0] == 403
    s, err = req(hub, "POST", "/api/v1/backup-all", {}, headers=auth)
    assert s == 400 and '"all"' in err["error"]
    s, body = req(hub, "POST", "/api/v1/backup-all", {"confirm": "all"}, headers=auth)
    assert s == 202 and body["skipped"] == []
    [started] = body["started"]
    assert started["machine"] == "alpha" and started["stacks"] == ["app"] and started["job"]["kind"] == "backup-all"
    job = wait_job(hub, started["job"]["id"])
    assert job.state == "ok" and job.result["stacks"]["app"]["state"] == "ok"
    assert hub.restic.latest("app").tag("trigger") == "manual"
    assert hub.notifier.heartbeats == []
    s, jobs = req(hub, "GET", "/api/v1/jobs?stack=", headers=auth)
    assert jobs["jobs"][0]["kind"] == "backup-all" and jobs["jobs"][0]["trigger"] == "manual"
    s, detail = req(hub, "GET", "/api/v1/stacks/app", headers=auth)
    assert detail["last"]["state"] == "ok"  # counts as the stack's last backup, like the nightly run

    # One agent: confirm with its name.
    s, err = req(hub, "POST", "/api/v1/backup-all", {"machine": "alpha", "confirm": "all"}, headers=auth)
    assert s == 400 and '"alpha"' in err["error"]
    s, body = req(hub, "POST", "/api/v1/backup-all", {"machine": "alpha", "confirm": "alpha"}, headers=auth)
    assert s == 202 and [x["machine"] for x in body["started"]] == ["alpha"]
    wait_job(hub, body["started"][0]["job"]["id"])
    assert req(hub, "POST", "/api/v1/backup-all", {"machine": "nope", "confirm": "nope"}, headers=auth)[0] == 404


def test_agent_backup_all_runs_one_at_a_time(tmp_path, repo):
    from backtrack.server import local_agent_call

    lab = Lab(tmp_path, repo, machine="alpha")
    service = Service(lab.cfg, lab.agent)  # runner not started: jobs stay queued
    first = local_agent_call(service, "POST", "/v1/backup-all", {})
    again = local_agent_call(service, "POST", "/v1/backup-all", {})
    assert "existing" not in first and again["existing"] and again["job"]["id"] == first["job"]["id"]
    assert first["job"]["params"] == {"trigger": "manual"}


def test_hub_backup_all_skips_agents_it_cannot_use(hub):
    from backtrack.hub import AgentUnreachable, Hub

    stack = [{"name": "app", "mode": "stop"}]
    fleet = [
        {"machine": "alpha", "online": True, "version": "0.3.0", "stacks": stack},
        {"machine": "beta", "online": False, "version": "0.3.0", "stacks": stack},
        {"machine": "gamma", "online": True, "version": "0.3.0", "stacks": []},
        {"machine": "delta", "online": True, "version": "0.2.2", "stacks": stack},
        {"machine": "eps", "online": True, "version": "0.3.0", "stacks": stack},
        {"machine": "zeta", "online": True, "version": "0.3.0", "stacks": stack},
    ]
    calls = []

    def call(machine, method, path, body=None):
        calls.append((machine, method, path))
        if machine == "eps":
            raise AgentUnreachable("eps: connection refused")
        return {"job": {"id": machine}, **({"existing": True} if machine == "zeta" else {})}

    h = Hub(hub.service, KEY)
    h.agents = lambda: fleet
    h.call = call
    res = h.backup_all()
    assert [s["machine"] for s in res["started"]] == ["alpha"]
    reasons = {s["machine"]: s["reason"] for s in res["skipped"]}
    assert reasons["beta"] == "offline" and reasons["gamma"] == "no stacks"
    assert "0.3.0" in reasons["delta"] and "refused" in reasons["eps"] and "already" in reasons["zeta"]
    assert {c[0] for c in calls} == {"alpha", "eps", "zeta"} and all(c[1:] == ("POST", "/v1/backup-all") for c in calls)

    assert [s["machine"] for s in h.backup_all("alpha")["started"]] == ["alpha"]
    with pytest.raises(LookupError):
        h.backup_all("nope")


def test_backup_all_panel_names_the_stacks_that_stop():
    from backtrack import ui

    agents = [{"machine": "alpha", "online": True, "version": "0.3.0", "stacks": [{"name": "db", "mode": "stop"},
                                                             {"name": "web", "mode": "live"}]},
              {"machine": "beta", "online": True, "job": {"kind": "backup"}, "stacks": [{"name": "app", "mode": "stop"}]},
              {"machine": "gamma", "online": False, "stacks": [{"name": "old", "mode": "stop"}]},
              {"machine": "delta", "online": True, "version": "0.2.2", "stacks": [{"name": "legacy", "mode": "stop"}]}]
    html = ui.backup_all(agents, "tok")
    assert "Stops while backed up: <span class=\"bt-code\">db</span>" in html
    assert "Stays up: <span class=\"bt-code\">web</span>" in html
    assert "queues behind the current job" in html and "Skipped, offline: gamma" in html
    assert "Skipped, older than 0.3.0: delta" in html and "legacy" not in html
    assert "Back up 3 stacks" in html and 'name="machine"' not in html
    one = ui.backup_all(agents, "tok", "beta")
    assert "every stack on beta" in one and 'name="machine" value="beta"' in one and "db" not in one
    assert ui.backup_all(agents, "tok", "gamma") == ""  # offline: no button
