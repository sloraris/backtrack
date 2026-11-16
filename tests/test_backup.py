import pytest

from backtrack.ops import OpError
from backtrack.stacks import L_KEEP, L_MODE, L_PATH, L_POST_START, L_PRE_STOP


def test_stop_mode_stops_backs_up_and_starts_in_dependency_order(lab):
    lab.stack_dir("app")
    lab.docker.add("app", "db", {L_MODE: "stop"})
    lab.docker.add("app", "web", {L_MODE: "stop"}, depends_on=["db"])

    result = lab.agent.backup("app", lab.job())

    assert lab.docker.calls == [("stop", "app-web"), ("stop", "app-db"), ("start", "app-db"), ("start", "app-web")]
    snaps = lab.restic.snapshots("app")
    assert len(snaps) == 1 and snaps[0].short_id == result["snapshot"]
    assert snaps[0].host == "app"
    assert set(snaps[0].tags) == {"stack=app", "machine=alpha", "trigger=manual"}
    assert snaps[0].paths == [str(lab.appdata / "app")]


def test_live_mode_does_not_stop(lab):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "live"})
    lab.agent.backup("app", lab.job())
    assert lab.docker.calls == []


def test_start_runs_even_when_backup_fails(lab, monkeypatch):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop"})
    from backtrack.restic import ResticError

    def broken(*a, **k):
        raise ResticError("disk on fire", "stderr tail")

    monkeypatch.setattr(lab.restic, "backup", broken)
    with pytest.raises(ResticError):
        lab.agent.backup("app", lab.job())
    assert lab.docker.calls == [("stop", "app-web"), ("start", "app-web")]
    assert lab.notifier.failures and lab.notifier.failures[0][1] == "backup"


def test_stopped_stack_stays_stopped(lab):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop"}, state="exited")
    lab.agent.backup("app", lab.job())
    assert lab.docker.calls == []
    assert len(lab.restic.snapshots("app")) == 1


def test_refuses_missing_or_empty_folder(lab):
    lab.docker.add("app", "web", {L_MODE: "stop"})
    with pytest.raises(OpError, match="missing or empty"):
        lab.agent.backup("app", lab.job())
    (lab.appdata / "app" / "data" / "sub").mkdir(parents=True)  # a tree of empty folders is still empty
    with pytest.raises(OpError, match="missing or empty"):
        lab.agent.backup("app", lab.job())
    assert lab.docker.calls == []
    assert lab.restic.snapshots("app") == []


def test_excludes_from_backupignore(lab):
    d = lab.stack_dir("app", {"keep.txt": "1", "cache/big.bin": "x" * 100, ".backupignore": "cache\n"})
    lab.docker.add("app", "web", {L_MODE: "live"})
    lab.agent.backup("app", lab.job())
    snap = lab.restic.latest("app")
    files, _ = lab.restic.ls_files(snap)
    assert files == 2  # keep.txt and .backupignore
    assert d.exists()


def test_ignore_patterns_never_match_folders_above_the_stack(lab):
    # restic alone would match `appdata` against <tmp>/appdata/app and exclude everything (found on a real host
    # with `cache/` and a stack under /opt/cache)
    lab.stack_dir("app", {"keep.txt": "1", "sub/appdata/x": "2", "logs/a.log": "3",
                          ".backupignore": f"appdata/\n*.log\n# comment\n{lab.appdata}/app/missing\n"})
    lab.docker.add("app", "web", {L_MODE: "live"})
    lab.agent.backup("app", lab.job())
    files, _ = lab.restic.ls_files(lab.restic.latest("app"))
    assert files == 2  # keep.txt and .backupignore; sub/appdata/ and *.log excluded at any depth


def test_custom_path_label(lab, tmp_path):
    custom = tmp_path / "elsewhere"
    (custom / "x").mkdir(parents=True)
    (custom / "x" / "f").write_text("1")
    lab.docker.add("app", "web", {L_MODE: "live", L_PATH: str(custom)})
    lab.agent.backup("app", lab.job())
    assert lab.restic.latest("app").paths == [str(custom)]


def test_retention_override_is_applied(lab, monkeypatch):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "live", L_KEEP: "daily=2"})
    seen = {}
    monkeypatch.setattr(lab.restic, "forget", lambda host, keep: seen.update(host=host, keep=keep))
    lab.agent.backup("app", lab.job())
    assert seen == {"host": "app", "keep": {"daily": 2}}


def test_hooks_called_and_failure_tolerated(lab, monkeypatch):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop", L_PRE_STOP: "http://silence.invalid/pre",
                                  L_POST_START: "http://silence.invalid/post"})
    posted = []

    def fake_post(url, payload, timeout=10, retries=1):
        posted.append((url, payload["event"], payload["ok"]))
        return url.endswith("post")  # pre-stop hook "fails"

    monkeypatch.setattr("backtrack.notify.post_json", fake_post)
    job = lab.job()
    lab.agent.backup("app", job)
    assert [p[:2] for p in posted] == [("http://silence.invalid/pre", "pre-stop"),
                                       ("http://silence.invalid/post", "post-start")]
    assert posted[1][2] is True
    assert any("WARNING: hook pre-stop" in line for line in job.lines)
    assert len(lab.restic.snapshots("app")) == 1


def test_start_failure_alerts(lab):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.docker.fail_start.add("app-web")
    with pytest.raises(OpError, match="did not start"):
        lab.agent.backup("app", lab.job())
    assert any(f[1] == "start" for f in lab.notifier.failures)


def test_nightly_continues_after_a_failure_and_reports(lab):
    lab.stack_dir("a")
    lab.docker.add("a", "web", {L_MODE: "live"})
    lab.docker.add("b", "web", {L_MODE: "live"})  # no folder -> fails
    lab.stack_dir("c")
    lab.docker.add("c", "web", {L_MODE: "stop"})
    lab.docker.add("ignored", "web", {})  # no label
    result = lab.agent.nightly(lab.job("nightly"))
    assert result["failed"] == ["b"]
    assert result["stacks"]["a"]["state"] == "ok" and result["stacks"]["c"]["state"] == "ok"
    assert lab.notifier.heartbeats == [(False, "2/3 stacks backed up; failed: b")]
    assert lab.notifier.summaries == [(["b"], 3)]
    assert lab.restic.latest("a").tag("trigger") == "nightly"


def test_misconfigured_stack_is_reported(lab):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.docker.add("app", "db", {L_MODE: "live"})
    with pytest.raises(OpError, match="disagree"):
        lab.agent.backup("app", lab.job())


def test_moved_stack_keeps_series_and_parent(tmp_path, repo):
    from tests.conftest import Lab

    appdata = tmp_path / "appdata"  # same host path on both machines, as in a real lab
    a = Lab(tmp_path, repo, machine="alpha")
    a.stack_dir("app", {"big.bin": "x" * 50_000})
    a.docker.add("app", "web", {L_MODE: "live"})
    first = a.restic.snapshot(a.agent.backup("app", a.job())["snapshot"])

    # Machine alpha dies; the stack is deployed on beta, auto-restored, then backed up there.
    import shutil
    shutil.rmtree(appdata / "app")
    b = Lab(tmp_path, repo, machine="beta")
    assert b.agent.auto_restore("app", b.job()) == 0
    b.docker.add("app", "web", {L_MODE: "live"})
    second = b.restic.snapshot(b.agent.backup("app", b.job())["snapshot"])

    assert [s.id for s in b.restic.snapshots("app")] == [first.id, second.id]
    assert second.tag("machine") == "beta"
    assert second.parent == first.id
