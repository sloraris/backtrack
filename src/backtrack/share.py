"""State shared between agents through the NFS share: presence files and the maintenance lease."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


@dataclass
class Lease:
    machine: str
    assigned_by: str  # "hub:<machine>" or "env"
    renewed_at: float

    def to_dict(self) -> dict:
        return {"machine": self.machine, "assigned_by": self.assigned_by, "renewed_at": self.renewed_at}


class Share:
    def __init__(self, root: Path, machine: str, presence_stale: float = 180, lease_stale_days: float = 8,
                 clock: Callable[[], float] = time.time):
        self.root = root
        self.machine = machine
        self.presence_stale = presence_stale
        self.lease_stale = lease_stale_days * 86400
        self.clock = clock

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def lease_path(self) -> Path:
        return self.root / "maintenance.json"

    # -- presence -------------------------------------------------------------------------------------------
    def write_presence(self, data: dict) -> None:
        _write_json(self.agents_dir / f"{self.machine}.json", {**data, "machine": self.machine,
                                                                "seen_at": self.clock()})

    def agents(self) -> list[dict]:
        out = []
        if not self.agents_dir.is_dir():
            return out
        now = self.clock()
        for f in sorted(self.agents_dir.glob("*.json")):
            data = _read_json(f)
            if not data or "machine" not in data:
                continue
            data["online"] = now - float(data.get("seen_at", 0)) <= self.presence_stale
            out.append(data)
        return out

    def hub_active(self) -> str | None:
        """Machine name of an online hub, if any."""
        for a in self.agents():
            if a["online"] and a.get("hub"):
                return a["machine"]
        return None

    def running_elsewhere(self, stack: str) -> list[str]:
        """Online agents other than this one that report `stack` with running containers."""
        out = []
        for a in self.agents():
            if a["machine"] == self.machine or not a["online"]:
                continue
            for s in a.get("stacks", []):
                if s.get("name") == stack and s.get("state") in ("running", "partial"):
                    out.append(a["machine"])
        return out

    # -- maintenance lease ----------------------------------------------------------------------------------
    def lease(self) -> Lease | None:
        data = _read_json(self.lease_path)
        if not data or not data.get("machine"):
            return None
        return Lease(data["machine"], data.get("assigned_by", ""), float(data.get("renewed_at", 0)))

    def lease_stale_now(self, lease: Lease) -> bool:
        return self.clock() - lease.renewed_at > self.lease_stale

    def assign_lease(self, machine: str, assigned_by: str) -> Lease:
        lease = Lease(machine, assigned_by, self.clock())
        _write_json(self.lease_path, lease.to_dict())
        return lease

    def holds_maintenance(self, wants: bool) -> bool:
        """Decide whether this agent is the maintenance agent right now, renewing or claiming the lease.

        - The lease names us: renew it and say yes.
        - A hub is online: only the hub assigns the lease, so no.
        - No hub: claim the lease if we want it (MAINTENANCE=true) and it is free or stale.
        """
        lease = self.lease()
        if lease and lease.machine == self.machine:
            self.assign_lease(self.machine, lease.assigned_by)
            return True
        if self.hub_active():
            return False
        if wants and (lease is None or self.lease_stale_now(lease)):
            self.assign_lease(self.machine, "env")
            # Two agents racing over NFS: whoever wrote last wins; re-read to agree.
            current = self.lease()
            return bool(current and current.machine == self.machine)
        return False
