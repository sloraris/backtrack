from backtrack.share import Share


def make(tmp_path, machine, clock):
    return Share(tmp_path / "share", machine, clock=lambda: clock[0])


def test_presence_online_and_stale(tmp_path):
    clock = [1000.0]
    a, b = make(tmp_path, "a", clock), make(tmp_path, "b", clock)
    a.write_presence({"hub": False})
    b.write_presence({"hub": True})
    assert {x["machine"]: x["online"] for x in a.agents()} == {"a": True, "b": True}
    assert a.hub_active() == "b"
    clock[0] += 181
    a.write_presence({"hub": False})
    assert {x["machine"]: x["online"] for x in a.agents()} == {"a": True, "b": False}
    assert a.hub_active() is None


def test_env_claim_without_hub(tmp_path):
    clock = [1000.0]
    a, b = make(tmp_path, "a", clock), make(tmp_path, "b", clock)
    assert a.holds_maintenance(wants=False) is False
    assert a.holds_maintenance(wants=True) is True
    assert a.lease().assigned_by == "env"
    # a second agent with MAINTENANCE=true is ignored while the lease is held
    assert b.holds_maintenance(wants=True) is False
    # the holder renews; after 8 days without renewal the lease is free again
    clock[0] += 7 * 86400
    assert a.holds_maintenance(wants=True) is True
    clock[0] += 8 * 86400 + 1
    assert b.holds_maintenance(wants=True) is True
    assert a.holds_maintenance(wants=True) is False


def test_hub_assigns_and_blocks_env_claims(tmp_path):
    clock = [1000.0]
    a, b, hub = make(tmp_path, "a", clock), make(tmp_path, "b", clock), make(tmp_path, "hub", clock)
    hub.write_presence({"hub": True})
    assert a.holds_maintenance(wants=True) is False  # hub online: no self-claims
    hub.assign_lease("b", "hub:hub")
    assert b.holds_maintenance(wants=False) is True  # assigned even without MAINTENANCE=true
    assert a.holds_maintenance(wants=True) is False
    # stale lease while the hub is online still isn't claimable by env
    clock[0] += 9 * 86400
    hub.write_presence({"hub": True})
    assert a.holds_maintenance(wants=True) is False


def test_running_elsewhere(tmp_path):
    clock = [1000.0]
    a, b = make(tmp_path, "a", clock), make(tmp_path, "b", clock)
    b.write_presence({"stacks": [{"name": "app", "state": "running"}, {"name": "old", "state": "stopped"}]})
    a.write_presence({"stacks": [{"name": "app", "state": "running"}]})
    assert a.running_elsewhere("app") == ["b"]
    assert a.running_elsewhere("old") == []
    assert b.running_elsewhere("app") == ["a"]
