"""Stacks whose containers are gone (`docker compose down`, or moved to another machine)."""

import json

import pytest

from backtrack.ops import OpError
from backtrack.stacks import KNOWN_FILE, L_KEEP, L_MODE, L_PATH
from tests.conftest import Lab


def down(lab, project):
    """What `docker compose down` does: the containers, and their labels, are gone."""
    lab.docker.items = [c for c in lab.docker.items if c.project != project]


def test_a_stack_taken_down_is_remembered_as_absent(lab, tmp_path):
    data = tmp_path / "elsewhere" / "app"
    data.mkdir(parents=True)
    (data / "f").write_text("x")
    lab.docker.add("app", "web", {L_MODE: "stop", L_PATH: str(data), L_KEEP: "daily=2"})
    assert lab.agent.find("app").state == "running"
    down(lab, "app")

    stack = lab.agent.find("app")
    assert stack.state == "absent" and stack.containers == []
    assert stack.mode == "stop" and stack.path == data and stack.keep == {"daily": 2}
    assert stack.last_seen is not None
    assert stack.to_dict()["state"] == "absent"


def test_absent_stack_backs_up_by_hand_without_stopping_anything(lab):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.agent.stacks()
    down(lab, "app")

    result = lab.agent.backup("app", lab.job(), "pre-migrate")

    assert lab.docker.calls == []
    snaps = lab.restic.snapshots("app")
    assert len(snaps) == 1 and snaps[0].short_id == result["snapshot"]
    assert "trigger=pre-migrate" in snaps[0].tags


def test_absent_stack_still_refuses_an_empty_folder(lab):
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.agent.stacks()
    down(lab, "app")
    with pytest.raises(OpError, match="missing or empty"):
        lab.agent.backup("app", lab.job())


def test_nightly_and_backup_all_skip_absent_stacks_but_a_bulk_selection_takes_them(lab):
    lab.stack_dir("app")
    lab.stack_dir("old")
    lab.docker.add("app", "web", {L_MODE: "live"})
    lab.docker.add("old", "web", {L_MODE: "live"})
    lab.agent.stacks()
    down(lab, "old")

    job = lab.job()
    res = lab.agent.nightly(job)
    assert res == {"stacks": {"app": res["stacks"]["app"]}, "failed": []}
    assert any("skipping old" in line for line in job.lines)
    assert lab.notifier.heartbeats[-1][0] is True
    assert lab.agent.backup_all(lab.job())["stacks"].keys() == {"app"}
    assert lab.restic.snapshots("old") == []

    res = lab.agent.backup_stacks(lab.job(), ["old"])
    assert res["stacks"]["old"]["state"] == "ok"


def test_absent_stack_refuses_while_another_agent_runs_it(tmp_path, repo):
    share = tmp_path / "share"
    a = Lab(tmp_path, repo, machine="alpha", share_root=share)
    b = Lab(tmp_path, repo, machine="beta", share_root=share)
    a.stack_dir("app")
    a.docker.add("app", "web", {L_MODE: "stop"})
    a.agent.stacks()
    down(a, "app")
    b.docker.add("app", "web", {L_MODE: "stop"})
    b.share.write_presence(b.agent.presence())

    with pytest.raises(OpError, match="running on beta"):
        a.agent.backup("app", a.job(), "pre-migrate")
    assert a.restic.snapshots("app") == []
    assert a.notifier.failures[-1][:2] == ("app", "backup")


def test_coming_back_up_replaces_the_remembered_copy(lab):
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.agent.stacks()
    down(lab, "app")
    lab.docker.add("app", "web", {L_MODE: "live"})
    stack = lab.agent.find("app")
    assert stack.state == "running" and stack.mode == "live"
    down(lab, "app")
    assert lab.agent.find("app").mode == "live"


def test_removing_the_mode_label_drops_the_remembered_copy(lab):
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.agent.stacks()
    lab.docker.items[0].labels.pop(L_MODE)
    assert lab.agent.find("app") is None
    down(lab, "app")
    assert lab.agent.find("app") is None


def test_a_broken_label_does_not_replace_the_remembered_copy(lab):
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.agent.stacks()
    lab.docker.items[0].labels[L_MODE] = "sometimes"
    assert lab.agent.find("app").error
    down(lab, "app")
    stack = lab.agent.find("app")
    assert stack.state == "absent" and stack.mode == "stop" and not stack.error


def test_untrack(lab):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop"})
    lab.agent.backup("app", lab.job())
    with pytest.raises(OpError, match="still has containers"):
        lab.agent.untrack("app")
    down(lab, "app")
    assert lab.agent.untrack("app") == {"untracked": "app"}
    assert lab.agent.find("app") is None
    assert len(lab.restic.snapshots("app")) == 1  # snapshots stay
    with pytest.raises(OpError, match="isn't tracked"):
        lab.agent.untrack("app")


def test_backup_with_a_path_for_a_folder_never_seen(lab, tmp_path):
    data = tmp_path / "loose"
    with pytest.raises(OpError, match="pass its path"):
        lab.agent.backup("loose", lab.job())
    with pytest.raises(OpError, match="missing or empty"):
        lab.agent.backup("loose", lab.job(), path=data)
    data.mkdir()
    (data / "f").write_text("x")
    lab.agent.backup("loose", lab.job(), path=data)
    snaps = lab.restic.snapshots("loose")
    assert len(snaps) == 1 and snaps[0].paths == [str(data)]
    with pytest.raises(OpError, match="absolute"):
        lab.agent.backup("loose", lab.job(), path=data.relative_to(tmp_path))
    with pytest.raises(OpError, match="invalid stack name"):
        lab.agent.backup("Bad Name", lab.job(), path=data)


def test_a_path_that_disagrees_with_a_known_stack_is_refused(lab, tmp_path):
    lab.stack_dir("app")
    lab.docker.add("app", "web", {L_MODE: "stop"})
    with pytest.raises(OpError, match="leave out the path"):
        lab.agent.backup("app", lab.job(), path=tmp_path)
    lab.agent.backup("app", lab.job(), path=lab.appdata / "app")


def test_unreadable_known_file_is_ignored(lab):
    lab.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (lab.cfg.state_dir / KNOWN_FILE).write_text("{not json")
    lab.docker.add("app", "web", {L_MODE: "stop"})
    assert [s.name for s in lab.agent.stacks()] == ["app"]
    saved = json.loads((lab.cfg.state_dir / KNOWN_FILE).read_text())
    assert saved["app"]["labels"] == {L_MODE: "stop"}
