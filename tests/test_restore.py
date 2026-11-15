import os
import shutil

import pytest

from backtrack.ops import OpError
from backtrack.restic import ResticError
from backtrack.stacks import L_MODE, L_POST_RESTORE

IS_ROOT = os.geteuid() == 0


def backed_up(lab, name="app", files=None, labels=None, **kw):
    d = lab.stack_dir(name, files)
    lab.docker.add(name, "web", {L_MODE: "stop", **(labels or {})}, **kw)
    snap = lab.agent.backup(name, lab.job())["snapshot"]
    lab.docker.calls.clear()
    return d, snap


def test_restore_in_place(lab):
    d, snap = backed_up(lab, files={"data/file.txt": "v1", "conf.yml": "a: 1"})
    (d / "data" / "file.txt").write_text("v2-broken")
    (d / "new-junk").write_text("junk")

    result = lab.agent.restore("app", lab.job(), "latest")

    assert (d / "data" / "file.txt").read_text() == "v1"
    assert not (d / "new-junk").exists()
    aside = d.with_name("app.pre-restore")
    assert (aside / "data" / "file.txt").read_text() == "v2-broken"
    assert result["snapshot"] == snap
    assert lab.docker.calls == [("stop", "app-web"), ("start", "app-web")]
    # safety snapshot of the broken state exists and is tagged
    safety = lab.restic.snapshot(result["safety_snapshot"])
    assert safety.tag("trigger") == "pre-restore"


def test_restore_specific_snapshot(lab):
    d, first = backed_up(lab, files={"f": "one"})
    (d / "f").write_text("two")
    lab.agent.backup("app", lab.job())
    lab.agent.restore("app", lab.job(), first)
    assert (d / "f").read_text() == "one"


def test_restore_rejects_snapshot_of_another_stack(lab):
    backed_up(lab, "app")
    _, other = backed_up(lab, "other")
    with pytest.raises(OpError, match="belongs to other"):
        lab.agent.restore("app", lab.job(), other)


def test_older_pre_restore_is_rotated_not_deleted(lab):
    d, _ = backed_up(lab)
    lab.agent.restore("app", lab.job())
    (d.with_name("app.pre-restore") / "marker").write_text("first")
    lab.agent.restore("app", lab.job())
    rotated = [p for p in lab.appdata.iterdir() if p.name.startswith("app.pre-restore.")]
    assert len(rotated) == 1 and (rotated[0] / "marker").read_text() == "first"
    assert d.with_name("app.pre-restore").is_dir()


def test_modes_and_owners_preserved(lab):
    d, _ = backed_up(lab, files={"secret/key": "k", "run.sh": "#!/bin/sh"})
    os.chmod(d / "secret" / "key", 0o600)
    os.chmod(d / "secret", 0o700)
    os.chmod(d / "run.sh", 0o755)
    if IS_ROOT:
        os.chown(d / "secret" / "key", 1234, 5678)
    lab.agent.backup("app", lab.job())
    lab.agent.restore("app", lab.job())
    assert (d / "secret" / "key").stat().st_mode & 0o777 == 0o600
    assert (d / "secret").stat().st_mode & 0o777 == 0o700
    assert (d / "run.sh").stat().st_mode & 0o777 == 0o755
    if IS_ROOT:
        st = (d / "secret" / "key").stat()
        assert (st.st_uid, st.st_gid) == (1234, 5678)


@pytest.mark.skipif(not IS_ROOT, reason="ownership needs root (CI runs this as root)")
def test_owner_of_restored_root_folder(lab):
    d, _ = backed_up(lab)
    os.chown(d, 1000, 1000)
    lab.agent.backup("app", lab.job())
    lab.agent.restore("app", lab.job())
    assert (d.stat().st_uid, d.stat().st_gid) == (1000, 1000)


def test_excluded_paths_are_carried_over(lab):
    d, _ = backed_up(lab, files={"panel/db.sqlite": "panel", "servers/world/level.dat": "WORLD",
                                 "logs/x.log": "log", ".backupignore": "servers/\n*.log\n"})
    lab.agent.restore("app", lab.job())
    assert (d / "servers" / "world" / "level.dat").read_text() == "WORLD"
    assert (d / "panel" / "db.sqlite").read_text() == "panel"
    # wildcard patterns are not carried over
    assert not (d / "logs" / "x.log").exists()
    assert (d.with_name("app.pre-restore") / "logs" / "x.log").exists()


def test_post_restore_runs_before_start(lab):
    order = []
    d, _ = backed_up(lab, labels={L_POST_RESTORE: "bash /srv/setup.sh"})
    lab.docker.on_host_command = lambda cmd, env: order.append(("host", cmd, env["BACKTRACK_STACK"]))
    orig_start = lab.docker.start
    lab.docker.start = lambda cid: (order.append(("start", cid)), orig_start(cid))
    lab.agent.restore("app", lab.job())
    assert order == [("host", "bash /srv/setup.sh", "app"), ("start", "app-web")]


def test_failure_after_set_aside_leaves_stack_stopped(lab, monkeypatch):
    d, _ = backed_up(lab)

    def broken(snap, target):
        raise ResticError("restore exploded")

    monkeypatch.setattr(lab.restic, "restore", broken)
    with pytest.raises(ResticError):
        lab.agent.restore("app", lab.job())
    assert lab.docker.calls == [("stop", "app-web")]
    assert d.with_name("app.pre-restore").is_dir()
    assert any("roll back" in f[2] for f in lab.notifier.failures)


def test_failed_post_restore_leaves_stack_stopped(lab):
    backed_up(lab, labels={L_POST_RESTORE: "false"})
    lab.docker.host_result = (1, "nope")
    with pytest.raises(OpError, match="post-restore"):
        lab.agent.restore("app", lab.job())
    assert lab.docker.calls == [("stop", "app-web")]


def test_failed_safety_snapshot_changes_nothing(lab, monkeypatch):
    d, _ = backed_up(lab, files={"f": "current"})

    def broken(*a, **k):
        raise ResticError("repo gone")

    monkeypatch.setattr(lab.restic, "backup", broken)
    with pytest.raises(ResticError):
        lab.agent.restore("app", lab.job())
    assert (d / "f").read_text() == "current"
    assert not d.with_name("app.pre-restore").exists()
    assert lab.docker.calls == [("stop", "app-web"), ("start", "app-web")]


def test_restore_without_containers_restores_files(lab):
    d, _ = backed_up(lab, files={"f": "x"})
    lab.docker.items.clear()
    shutil.rmtree(d)
    lab.agent.restore("app", lab.job())
    assert (d / "f").read_text() == "x"


# -- auto-restore ---------------------------------------------------------------------------------------------

def test_auto_restore_noop_when_folder_has_data(lab):
    d, _ = backed_up(lab, files={"f": "old"})
    (d / "f").write_text("live data")
    assert lab.agent.auto_restore("app", lab.job()) == 0
    assert (d / "f").read_text() == "live data"


def test_auto_restore_missing_folder(lab):
    d, _ = backed_up(lab, files={"data/f": "saved"})
    shutil.rmtree(d)
    assert lab.agent.auto_restore("app", lab.job()) == 0
    assert (d / "data" / "f").read_text() == "saved"


def test_auto_restore_folder_with_only_empty_dirs(lab):
    d, _ = backed_up(lab, files={"data/f": "saved"})
    shutil.rmtree(d)
    (d / "data").mkdir(parents=True)  # a pre-deploy step ran `mkdir -p`
    assert lab.agent.auto_restore("app", lab.job()) == 0
    assert (d / "data" / "f").read_text() == "saved"


def test_auto_restore_no_snapshot_starts_fresh(lab):
    job = lab.job()
    assert lab.agent.auto_restore("brand-new", job) == 0
    assert any("starts fresh" in line for line in job.lines)
    assert not (lab.appdata / "brand-new").exists()


def test_auto_restore_repo_unreachable_fails_closed(lab):
    lab.restic.repository = str(lab.tmp / "no-such-repo")
    assert lab.agent.auto_restore("app", lab.job()) == 1
    assert lab.notifier.failures


def test_auto_restore_split_brain_guard(lab):
    d, _ = backed_up(lab)
    shutil.rmtree(d)
    other = type(lab.share)(lab.share.root, "beta", clock=lambda: lab.clock[0])
    other.write_presence({"stacks": [{"name": "app", "state": "running"}]})
    job = lab.job()
    assert lab.agent.auto_restore("app", job) == 1
    assert any("still running on beta" in line for line in job.lines)
    assert not d.exists()
    # once beta's presence goes stale (agent gone), the guard no longer blocks
    lab.clock[0] += 600
    assert lab.agent.auto_restore("app", lab.job()) == 0


def test_auto_restore_custom_path_and_post_restore(lab, tmp_path):
    d, _ = backed_up(lab, files={"f": "x"}, labels={L_POST_RESTORE: "echo hi"})
    target = tmp_path / "other-place"
    assert lab.agent.auto_restore("app", lab.job(), path=target) == 0
    assert (target / "f").read_text() == "x"
    assert lab.docker.host_commands and lab.docker.host_commands[0][0] == "echo hi"


def test_auto_restore_rejects_bad_names(lab):
    assert lab.agent.auto_restore("../etc", lab.job()) == 2


# -- restore test, maintenance ----------------------------------------------------------------------------------

def test_restore_test_passes_and_cleans_up(lab):
    backed_up(lab, files={"a": "1", "b/c": "22"})
    result = lab.agent.restore_test(lab.job())
    assert result["stack"] == "app" and result["files"] == 2
    assert not (lab.cfg.scratch / "restore-test" / "app").exists()


def test_restore_test_alerts_on_mismatch(lab, monkeypatch):
    backed_up(lab)
    monkeypatch.setattr(lab.restic, "ls_files", lambda snap: (99, 1))
    with pytest.raises(OpError):
        lab.agent.restore_test(lab.job())
    assert lab.notifier.failures[-1][1] == "restore test"


def test_prune_only_on_maintenance_agent(lab):
    backed_up(lab)
    assert lab.agent.prune(lab.job()) == {"skipped": True}
    assert lab.agent.prune(lab.job(), force=True) == {"pruned": True}
    assert lab.agent.check(lab.job(), force=True, subset="1/12") == {"checked": "1/12"}


def test_forget_stack(lab):
    backed_up(lab)
    assert lab.agent.forget_stack("app", lab.job()) == {"forgotten": 1}
    assert lab.restic.snapshots("app") == []


def test_mode_of_restored_root_folder(lab):
    d, _ = backed_up(lab)
    os.chmod(d, 0o750)
    lab.agent.backup("app", lab.job())
    lab.agent.restore("app", lab.job())
    assert d.stat().st_mode & 0o7777 == 0o750


def test_auto_restore_root_folder_mode(lab):
    d, _ = backed_up(lab)
    os.chmod(d, 0o710)
    lab.agent.backup("app", lab.job())
    shutil.rmtree(d)
    assert lab.agent.auto_restore("app", lab.job()) == 0
    assert d.stat().st_mode & 0o7777 == 0o710
