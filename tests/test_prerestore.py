import json
import time
from pathlib import Path

import pytest

from backtrack.docker import Container, Health, parse_docker_time
from backtrack.ops import OpError
from backtrack.prerestore import Aside, Registry, is_aside_of, judge, parse_duration
from backtrack.stacks import L_MODE, L_POST_RESTORE


def restored(lab, name="app", labels=None):
    """Back up and restore a stack; returns its folder and the set-aside folder."""
    d = lab.stack_dir(name, {"data/file.txt": "v1"})
    lab.docker.add(name, "web", {L_MODE: "stop", **(labels or {})})
    lab.agent.backup(name, lab.job())
    (d / "data" / "file.txt").write_text("v2")
    lab.agent.restore(name, lab.job())
    return d, d.with_name(name + ".pre-restore")


def later(lab, seconds=3600):
    lab.agent.clock = lambda: time.time() + seconds


# -- units --------------------------------------------------------------------------------------------------------

def test_parse_duration():
    assert parse_duration("") == 0 and parse_duration("0") == 0
    assert parse_duration("300") == 300 and parse_duration("5m") == 300 and parse_duration("12h") == 43200
    assert parse_duration("7d") == 604800 and parse_duration("2w") == 1209600
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_parse_docker_time():
    assert parse_docker_time("0001-01-01T00:00:00Z") is None
    assert parse_docker_time("2026-01-02T03:04:05.123456789Z") == pytest.approx(1767323045.123456)
    assert parse_docker_time("junk") is None


def test_is_aside_of():
    t = Path("/opt/appdata/app")
    assert is_aside_of(Path("/opt/appdata/app.pre-restore"), t)
    assert is_aside_of(Path("/opt/appdata/app.pre-restore.20260102T030405"), t)
    assert not is_aside_of(Path("/opt/appdata/app"), t)
    assert not is_aside_of(Path("/opt/appdata/other.pre-restore"), t)
    assert not is_aside_of(Path("/srv/app.pre-restore"), t)
    assert not is_aside_of(Path("/opt/appdata/app.pre-restore.x"), t)


class StubDocker:
    def __init__(self, health):
        self.h = health

    def inspect(self, cid):
        return self.h[cid]


def test_judge_states():
    now = 10_000.0
    a = Aside("app", "/a/app.pre-restore", "/a/app", created=now - 1000, restore_ok=True, restored_at=now - 1000)
    c = [Container("1", "app-web-1", "app", "web", "running")]
    up = lambda **k: StubDocker({"1": Health(**{"running": True, "health": None, "started_at": now - 900, **k})})
    assert judge(a, c, up(), 300, now).state == "safe"
    assert judge(a, c, up(health="healthy"), 300, now).removable
    assert judge(a, c, up(health="starting"), 300, now).state == "settling"
    assert judge(a, c, up(health="unhealthy"), 300, now).state == "unhealthy"
    assert judge(a, c, up(running=False), 300, now).state == "unhealthy"
    assert judge(a, c, up(started_at=now - 60), 300, now).state == "settling"  # restarted a minute ago
    assert judge(a, None, up(), 300, now).state == "safe"  # files-only restore
    failed = Aside("app", "/a/app.pre-restore", "/a/app", created=now - 1000)
    assert judge(failed, c, up(), 300, now).state == "failed"


# -- recording ----------------------------------------------------------------------------------------------------

def test_restore_records_the_folder(lab):
    _, aside = restored(lab)
    [f] = lab.agent.prerestore()
    assert f["path"] == str(aside) and f["stack"] == "app" and f["restore_ok"] and f["size"] > 0
    assert f["safety_snapshot"] and f["state"] == "settling" and not f["removable"]
    later(lab)
    assert lab.agent.prerestore()[0]["state"] == "safe"


def test_rotation_keeps_the_record(lab):
    d, aside = restored(lab)
    lab.agent.restore("app", lab.job())
    paths = sorted(f["path"] for f in lab.agent.prerestore())
    assert len(paths) == 2 and str(aside) in paths
    assert any(Path(p).name.startswith("app.pre-restore.") and Path(p).is_dir() for p in paths)


def test_folder_removed_by_hand_drops_its_record(lab):
    import shutil
    _, aside = restored(lab)
    shutil.rmtree(aside)
    assert lab.agent.prerestore() == []


def test_failed_restore_is_never_removable(lab):
    lab.docker.host_result = (1, "nope\n")
    with pytest.raises(OpError):
        restored(lab, labels={L_POST_RESTORE: "false"})
    later(lab)
    [f] = lab.agent.prerestore()
    assert f["state"] == "failed" and not f["removable"]
    with pytest.raises(OpError, match="isn't healthy"):
        lab.agent.remove_prerestore("app", f["path"], lab.job())
    assert Path(f["path"]).is_dir()


def test_presence_carries_the_folders(lab):
    restored(lab)
    assert lab.agent.presence()["prerestore"][0]["stack"] == "app"


# -- removing -----------------------------------------------------------------------------------------------------

def test_remove_waits_for_the_stack_to_settle(lab):
    _, aside = restored(lab)
    with pytest.raises(OpError, match="isn't healthy"):
        lab.agent.remove_prerestore("app", str(aside), lab.job())
    later(lab)
    result = lab.agent.remove_prerestore("app", str(aside), lab.job())
    assert result["removed"] == str(aside) and not aside.exists()
    assert lab.agent.prerestore() == []


def test_remove_refuses_an_unhealthy_stack(lab):
    _, aside = restored(lab)
    lab.docker.health["app-web"] = "unhealthy"
    later(lab)
    with pytest.raises(OpError, match="unhealthy"):
        lab.agent.remove_prerestore("app", str(aside), lab.job())
    assert aside.is_dir()


def test_remove_refuses_paths_it_did_not_record(lab):
    d, aside = restored(lab)
    later(lab)
    stray = lab.appdata / "app.pre-restore.20200101T000000"
    stray.mkdir()
    for path in (str(stray), str(d), "/etc"):
        with pytest.raises(OpError, match="not a folder Backtrack set aside"):
            lab.agent.remove_prerestore("app", path, lab.job())
    with pytest.raises(OpError, match="not a folder Backtrack set aside"):
        lab.agent.remove_prerestore("other", str(aside), lab.job())
    assert stray.is_dir() and d.is_dir() and aside.is_dir()


def test_remove_refuses_a_tampered_record(lab):
    d, aside = restored(lab)
    later(lab)
    lab.agent.asides.update(str(aside), path=str(d))
    with pytest.raises(OpError, match="refusing"):
        lab.agent.remove_prerestore("app", str(d), lab.job())
    assert d.is_dir()


def test_remove_keeps_the_folder_without_its_safety_snapshot(lab):
    _, aside = restored(lab)
    later(lab)
    safety = lab.agent.prerestore()[0]["safety_snapshot"]
    lab.restic.forget_ids([lab.restic.snapshot(safety).id])
    with pytest.raises(OpError, match="safety snapshot .* is missing"):
        lab.agent.remove_prerestore("app", str(aside), lab.job())
    assert aside.is_dir()


def test_auto_remove(tmp_path, repo):
    from tests.conftest import Lab
    lab = Lab(tmp_path, repo, extra_env={"PRERESTORE_AUTO_REMOVE": "1d"})
    _, aside = restored(lab)
    later(lab, 3600)
    assert lab.agent.auto_remove_prerestore(lab.job())["removed"] == []  # too young
    later(lab, 2 * 86400)
    assert lab.agent.auto_remove_prerestore(lab.job())["removed"] == [str(aside)]
    assert not aside.exists()


def test_auto_remove_is_off_by_default(lab):
    _, aside = restored(lab)
    later(lab, 365 * 86400)
    assert lab.agent.auto_remove_prerestore(lab.job()) == {"removed": []}
    assert aside.is_dir()


# -- upgrading ----------------------------------------------------------------------------------------------------

def test_adopts_folders_from_earlier_restores(tmp_path):
    appdata = tmp_path / "appdata"
    (appdata / "app.pre-restore").mkdir(parents=True)
    (appdata / "app.pre-restore" / "f").write_text("x")
    jobs = [
        {"id": "old", "kind": "restore", "stack": "app", "state": "ok", "started": 1, "finished": 2,
         "result": {"snapshot": "aaaa1111", "set_aside": str(appdata / "app.pre-restore")}},
        {"id": "new", "kind": "restore", "stack": "app", "state": "ok", "started": 3, "finished": 4,
         "result": {"snapshot": "bbbb2222", "safety_snapshot": "cccc3333",
                    "set_aside": str(appdata / "app.pre-restore")}},
        {"id": "gone", "kind": "restore", "stack": "db", "state": "ok", "finished": 5,
         "result": {"set_aside": str(appdata / "db.pre-restore")}},
        {"id": "b", "kind": "backup", "stack": "app", "state": "ok", "finished": 6, "result": {}},
    ]
    reg = Registry(tmp_path / "state")
    assert reg.adopt_history(jobs) == 1
    [a] = reg.all()
    assert (a.job, a.safety_snapshot, a.target, a.restore_ok) == ("new", "cccc3333", str(appdata / "app"), True)
    assert reg.adopt_history(jobs) == 0  # only once
