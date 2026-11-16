from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backtrack.config import Config, ConfigError, parse_keep
from backtrack.docker import Container, demux_logs, parse_depends_on
from backtrack.schedule import next_run
from backtrack.signing import Verifier, sign
from backtrack.stacks import L_MODE, Stack, discover, has_files, literal_ignores, literal_matches, restic_excludes

TZ = ZoneInfo("America/Boise")


# -- config ---------------------------------------------------------------------------------------------------

def base_env(tmp_path, **kw):
    return {"MACHINE": "echo", "RESTIC_PASSWORD": "pw", "STATE_DIR": str(tmp_path), **kw}


def test_env_locks_and_ui_fills_gaps(tmp_path):
    cfg = Config.load(base_env(tmp_path, BACKUP_SCHEDULE="03:30"))
    assert cfg.get("BACKUP_SCHEDULE") == "03:30" and cfg.locked("BACKUP_SCHEDULE")
    assert cfg.update_settings({"BACKUP_SCHEDULE": "05:00"}) == ["BACKUP_SCHEDULE is set in the environment"]
    assert cfg.update_settings({"KEEP": "daily=3"}) == []
    assert cfg.get("KEEP") == "daily=3"
    # saved settings survive a restart; env still wins
    again = Config.load(base_env(tmp_path, BACKUP_SCHEDULE="03:30"))
    assert again.get("KEEP") == "daily=3" and again.get("BACKUP_SCHEDULE") == "03:30"
    sources = {s["key"]: s["source"] for s in again.settings()}
    assert sources["KEEP"] == "saved" and sources["BACKUP_SCHEDULE"] == "env" and sources["MAINTENANCE"] == "default"


def test_settings_validation(tmp_path):
    cfg = Config.load(base_env(tmp_path))
    errs = cfg.update_settings({"KEEP": "forever", "PRUNE_SCHEDULE": "sun 13:00", "RESTIC_PASSWORD": "x"})
    assert len(errs) == 2 and cfg.get("PRUNE_SCHEDULE") == "sun 13:00"  # nothing saved on error
    assert cfg.saved == {}


def test_secret_files(tmp_path):
    f = tmp_path / "pw"
    f.write_text("from-file\n")
    cfg = Config.load({"MACHINE": "echo", "RESTIC_PASSWORD_FILE": str(f), "STATE_DIR": str(tmp_path)})
    assert cfg.get("RESTIC_PASSWORD") == "from-file"


def test_validate(tmp_path):
    with pytest.raises(ConfigError):
        Config.load({"RESTIC_PASSWORD": "x", "STATE_DIR": str(tmp_path)}).validate()
    with pytest.raises(ConfigError, match="UI_PASSWORD"):
        Config.load(base_env(tmp_path, HUB="true")).validate()
    Config.load(base_env(tmp_path)).validate()


def test_parse_keep():
    assert parse_keep("daily=7,weekly=4,monthly=6") == {"daily": 7, "weekly": 4, "monthly": 6}
    with pytest.raises(ConfigError):
        parse_keep("daily=7;weekly=4")


# -- discovery ------------------------------------------------------------------------------------------------

def c(project, service, labels=None, state="running", deps=None):
    return Container(f"{project}-{service}", f"{project}-{service}-1", project, service, state,
                     {"com.docker.compose.project": project, **(labels or {})}, deps or [])


def test_discovery_and_order(tmp_path):
    stacks = discover([
        c("app", "web", {L_MODE: "stop"}, deps=["api"]), c("app", "api", deps=["db"]), c("app", "db"),
        c("other", "x"), c("Bad Name", "x", {L_MODE: "live"}), c("weird", "x", {L_MODE: "sometimes"}),
    ], tmp_path, {"daily": 7})
    by = {s.name: s for s in stacks}
    assert set(by) == {"app", "Bad Name", "weird"}
    assert by["app"].path == tmp_path / "app" and by["app"].error is None
    assert [x.service for x in by["app"].start_order()] == ["db", "api", "web"]
    assert [x.service for x in by["app"].stop_order()] == ["web", "api", "db"]
    assert by["Bad Name"].error and by["weird"].error


def test_dependency_cycle_does_not_hang():
    s = Stack("x", containers=[c("x", "a", deps=["b"]), c("x", "b", deps=["a"])])
    assert {x.service for x in s.start_order()} == {"a", "b"}


def test_has_files(tmp_path):
    assert not has_files(tmp_path / "missing")
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / ".backupignore").write_text("x")
    assert not has_files(tmp_path)
    (tmp_path / "a" / "b" / "f").write_text("1")
    assert has_files(tmp_path)


def test_literal_ignores(tmp_path):
    f = tmp_path / ".backupignore"
    f.write_text(f"# comment\nservers/\n*.log\n!keep\ncache\n{tmp_path}/abs/dir\n/etc/elsewhere\n../escape\n")
    assert [str(p) for p in literal_ignores(f, tmp_path)] == ["servers", "cache", "abs/dir"]


def test_docker_helpers():
    assert parse_depends_on("db:service_healthy:false,cache:service_started:true") == ["db", "cache"]
    frame = b"\x01\x00\x00\x00\x00\x00\x00\x03hi\n" + b"\x02\x00\x00\x00\x00\x00\x00\x04err\n"
    assert demux_logs(frame) == "hi\nerr\n"


# -- schedule -------------------------------------------------------------------------------------------------

def test_daily_next_run():
    now = datetime(2026, 11, 14, 3, 59, tzinfo=TZ)
    assert next_run("04:00", now, TZ, "daily") == datetime(2026, 11, 14, 4, 0, tzinfo=TZ)
    now = datetime(2026, 11, 14, 4, 0, tzinfo=TZ)
    assert next_run("04:00", now, TZ, "daily") == datetime(2026, 11, 15, 4, 0, tzinfo=TZ)


def test_daily_across_dst():
    # 2026-03-08 spring forward at 02:00 in America/Boise; 2026-11-01 fall back.
    before = datetime(2026, 3, 7, 12, 0, tzinfo=TZ)
    run = next_run("04:00", before, TZ, "daily")
    assert (run.month, run.day, run.hour) == (3, 8, 4) and run.utcoffset().total_seconds() == -6 * 3600
    gap = next_run("02:30", before, TZ, "daily")  # 02:30 doesn't exist that day
    assert gap.day == 8 and gap.hour == 3
    fall = next_run("04:00", datetime(2026, 10, 31, 12, 0, tzinfo=TZ), TZ, "daily")
    assert (fall.day, fall.hour) == (1, 4) and fall.utcoffset().total_seconds() == -7 * 3600


def test_weekly_and_monthly():
    sat = datetime(2026, 11, 14, 12, 0, tzinfo=TZ)  # a Saturday
    assert next_run("sun 13:00", sat, TZ, "weekly") == datetime(2026, 11, 15, 13, 0, tzinfo=TZ)
    assert next_run("1 14:00", sat, TZ, "monthly") == datetime(2026, 12, 1, 14, 0, tzinfo=TZ)
    dec = datetime(2026, 12, 2, 0, 0, tzinfo=TZ)
    assert next_run("1 14:00", dec, TZ, "monthly") == datetime(2027, 1, 1, 14, 0, tzinfo=TZ)


# -- signing --------------------------------------------------------------------------------------------------

def test_signing():
    clock = [1_000_000.0]
    v = Verifier("k" * 32, clock=lambda: clock[0])
    h = sign("k" * 32, "POST", "/v1/stacks/app/backup", b'{"a":1}', now=clock[0])
    assert v.verify("POST", "/v1/stacks/app/backup", h, b'{"a":1}')
    assert not v.verify("POST", "/v1/stacks/app/backup", h, b'{"a":1}'), "replay"
    h2 = sign("k" * 32, "POST", "/v1/stacks/app/backup", b'{"a":1}', now=clock[0])
    assert not v.verify("POST", "/v1/stacks/app/restore", h2, b'{"a":1}'), "other path"
    assert not v.verify("POST", "/v1/stacks/app/backup", h2, b'{"a":2}'), "other body"
    h3 = sign("wrong", "GET", "/v1/status", now=clock[0])
    assert not v.verify("GET", "/v1/status", h3, b"")
    old = sign("k" * 32, "GET", "/v1/status", now=clock[0] - 61)
    assert not v.verify("GET", "/v1/status", old, b""), "expired"
    assert not Verifier("").verify("GET", "/v1/status", sign("", "GET", "/v1/status"), b"")


def test_go_mode_to_unix():
    from backtrack.restic import go_mode_to_unix
    assert go_mode_to_unix(2147484136) == 0o750  # dir bit + 0750, as restic prints it
    assert go_mode_to_unix((1 << 31) | (1 << 22) | 0o775) == 0o2775  # setgid dir
    assert go_mode_to_unix((1 << 20) | 0o777) == 0o1777  # sticky


def test_docker_api_version_comes_from_the_daemon():
    import http.server
    import socketserver
    import tempfile
    import threading

    from backtrack.docker import Docker

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = b"OK" if self.path == "/_ping" else b"[]"
            self.send_response(200)
            self.send_header("Api-Version", "1.52")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

        def address_string(self):
            return "unix"

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    sock = tempfile.mkdtemp(dir="/tmp") + "/d.sock"
    srv = Server(sock, Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = Docker(sock)
        assert d.containers() == [] and d.containers() == []
        assert seen == ["/_ping", "/v1.52/containers/json?all=1", "/v1.52/containers/json?all=1"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_restic_excludes_are_anchored(tmp_path):
    root = tmp_path / "st[1]"
    root.mkdir()
    f = root / ".backupignore"
    f.write_text("# c\ncache/\n*.log\ndata/tmp\n!keep.log\n/etc/abs\n\n")
    r = str(root).replace("[", "\\[").replace("]", "\\]")
    assert restic_excludes(f, root) == [f"{r}/**/cache/", f"{r}/**/*.log", f"{r}/data/tmp", f"!{r}/**/keep.log",
                                        "/etc/abs"]
    assert restic_excludes(root / "missing", root) == []


def test_literal_matches_at_any_depth(tmp_path):
    for rel in ("cache/a", "x/cache/b", "x/cache/cache/c", "data/tmp/d", "other/f"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("1")
    found = literal_matches(tmp_path, [Path("cache"), Path("data/tmp"), Path("nope/x")])
    assert [str(p) for p in found] == ["cache", "data/tmp", "x/cache"]
