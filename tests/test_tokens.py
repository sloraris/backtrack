import json
import stat
import urllib.parse
import urllib.request

import pytest

from backtrack import cli
from backtrack.tokens import TOUCH_EVERY, TokenStore
from tests.test_server import hub, req  # noqa: F401 - the hub fixture


def test_store_keeps_only_a_hash(tmp_path):
    store = TokenStore(tmp_path)
    tok, secret = store.create("dashboard", "read")
    assert secret.startswith("bt_") and len(secret) > 40
    raw = (tmp_path / "tokens.json").read_text()
    assert secret not in raw and tok.hash in raw
    assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600
    assert "hash" not in tok.public()
    assert store.check(secret) == "read"
    assert store.check(secret + "x") is None
    assert store.check("read-tok") is None


def test_store_validates_and_revokes(tmp_path):
    store = TokenStore(tmp_path)
    with pytest.raises(ValueError):
        store.create("", "read")
    with pytest.raises(ValueError):
        store.create("ok", "admin")
    with pytest.raises(ValueError):
        store.create("bad/name", "read")
    a, sa = store.create("one", "read")
    b, sb = store.create("two", "write")
    with pytest.raises(ValueError):
        store.create("one", "write")
    assert store.check(sb) == "write"
    assert store.revoke(a.id).name == "one"
    assert store.check(sa) is None and store.check(sb) == "write"
    assert store.revoke("two").id == b.id  # by name too
    assert store.revoke("two") is None and store.list() == []


def test_last_used_is_recorded_at_most_once_a_minute(tmp_path):
    clock = [1000.0]
    store = TokenStore(tmp_path, clock=lambda: clock[0])
    tok, secret = store.create("monitor", "read")
    assert store.list()[0].last_used is None
    store.check(secret)
    assert store.list()[0].last_used == 1000.0
    clock[0] += TOUCH_EVERY - 1
    store.check(secret)
    assert store.list()[0].last_used == 1000.0
    clock[0] += 1
    store.check(secret)
    assert store.list()[0].last_used == 1000.0 + TOUCH_EVERY


def test_a_second_store_sees_changes(tmp_path):
    """The CLI edits the file while the server runs; the server must notice."""
    server, cli_store = TokenStore(tmp_path), TokenStore(tmp_path)
    _, secret = cli_store.create("script", "write")
    assert server.check(secret) == "write"
    cli_store.revoke("script")
    assert server.check(secret) is None


def test_cli_create_list_revoke(tmp_path, monkeypatch, capsys):
    for k, v in {"MACHINE": "alpha", "RESTIC_PASSWORD": "x", "RESTIC_REPOSITORY": str(tmp_path / "repo"),
                 "STATE_DIR": str(tmp_path / "state"), "HUB": "true", "UI_PASSWORD": "pw",
                 "CLUSTER_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["token", "create", "nightly-report", "--scope", "write"]) == 0
    secret = capsys.readouterr().out.strip()
    assert TokenStore(tmp_path / "state").check(secret) == "write"
    assert cli.main(["token", "list"]) == 0
    out = capsys.readouterr().out
    assert "nightly-report" in out and "write" in out and secret not in out
    assert cli.main(["token", "create", "nightly-report"]) == 1
    assert cli.main(["token", "revoke", "nightly-report"]) == 0
    assert cli.main(["token", "revoke", "nightly-report"]) == 1
    assert TokenStore(tmp_path / "state").check(secret) is None


def test_named_tokens_on_the_hub_api(hub):  # noqa: F811
    store = TokenStore(hub.cfg.state_dir)
    _, read = store.create("dashboard", "read")
    _, write = store.create("script", "write")
    assert req(hub, "GET", "/api/v1/status", headers={"Authorization": f"Bearer {read}"})[0] == 200
    s, _ = req(hub, "POST", "/api/v1/stacks/app/backup", {"trigger": "manual"},
               headers={"Authorization": f"Bearer {read}"})
    assert s == 403
    s, body = req(hub, "POST", "/api/v1/stacks/app/backup", {"trigger": "manual"},
                  headers={"Authorization": f"Bearer {write}"})
    assert s == 202
    assert store.list()[0].last_used is not None
    # The env tokens keep working next to the named ones.
    assert req(hub, "GET", "/api/v1/status", headers={"Authorization": "Bearer read-tok"})[0] == 200
    store.revoke("dashboard")
    assert req(hub, "GET", "/api/v1/status", headers={"Authorization": f"Bearer {read}"})[0] == 401
    assert req(hub, "GET", "/api/v1/status", headers={"Authorization": "Bearer "})[0] == 401


def _signed_in(hub):  # noqa: F811
    r = urllib.request.Request(hub.base + "/login", data=b"password=correct+horse", method="POST")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        urllib.request.build_opener(NoRedirect).open(r, timeout=10)
    except urllib.error.HTTPError as e:
        cookie = e.headers["Set-Cookie"].split(";")[0]
    opener = urllib.request.build_opener()
    opener.addheaders = [("Cookie", cookie)]
    return opener


def test_settings_page_mints_and_revokes(hub):  # noqa: F811
    opener = _signed_in(hub)

    def post(path, form):
        data = urllib.parse.urlencode(form).encode()
        with opener.open(urllib.request.Request(hub.base + path, data=data, method="POST"), timeout=10) as r:
            return r.geturl(), r.read().decode()

    with opener.open(hub.base + "/settings", timeout=10) as r:
        html = r.read().decode()
    assert "API tokens" in html and "READ_TOKEN" in html and 'href="/settings"' in html
    csrf = html.split('name="csrf" value="')[1].split('"')[0]

    url, html = post("/settings/tokens", {"name": "dashboard", "scope": "read", "csrf": csrf})
    assert url.endswith("/settings/tokens") and "Token dashboard created" in html
    secret = html.split('aria-label="New token"')[0].rsplit('value="', 1)[1].split('"')[0]
    assert secret.startswith("bt_")
    assert req(hub, "GET", "/api/v1/stacks", headers={"Authorization": f"Bearer {secret}"})[0] == 200

    with opener.open(hub.base + "/settings", timeout=10) as r:
        html = r.read().decode()
    assert "dashboard" in html and secret not in html  # shown once

    _, html = post("/settings/tokens", {"name": "dashboard", "scope": "read", "csrf": csrf})
    assert "already exists" in html

    tok = TokenStore(hub.cfg.state_dir).list()[0]
    url, html = post(f"/settings/tokens/{tok.id}/revoke", {"csrf": csrf})
    assert "done=revoked" in url and "Token revoked" in html
    assert req(hub, "GET", "/api/v1/stacks", headers={"Authorization": f"Bearer {secret}"})[0] == 401

    # Without the CSRF token nothing is created.
    with pytest.raises(urllib.error.HTTPError) as err:
        post("/settings/tokens", {"name": "sneaky", "scope": "write"})
    assert err.value.code == 403 and TokenStore(hub.cfg.state_dir).list() == []


def test_tokens_cannot_reach_the_settings_page(hub):  # noqa: F811
    store = TokenStore(hub.cfg.state_dir)
    _, write = store.create("script", "write")
    data = urllib.parse.urlencode({"name": "x", "scope": "write"}).encode()
    r = urllib.request.Request(hub.base + "/settings/tokens", data=data, method="POST",
                               headers={"Authorization": f"Bearer {write}"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.build_opener(NoRedirect).open(r, timeout=10)
    assert err.value.code == 303 and err.value.headers["Location"] == "/login"
    assert [t.name for t in store.list()] == ["script"]
    assert json.loads((hub.cfg.state_dir / "tokens.json").read_text())["tokens"][0]["name"] == "script"
