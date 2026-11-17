"""Hub side: reads the fleet from presence files and calls agents' signed APIs."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from backtrack.restic import ResticError
from backtrack.signing import sign


class AgentUnreachable(Exception):
    pass


class Hub:
    def __init__(self, service, cluster_key: str, opener: Callable | None = None):
        self.service = service
        self.cfg = service.cfg
        self.share = service.agent.share
        self.cluster_key = cluster_key
        self._open = opener or urllib.request.urlopen
        self._snap_cache: tuple[float, list] | None = None

    # -- fleet ----------------------------------------------------------------------------------------------
    def agents(self) -> list[dict]:
        agents = {a["machine"]: a for a in self.share.agents()}
        # The hub's own entry comes live, not from its last presence write.
        agents[self.cfg.machine] = {**self.service.presence(), "machine": self.cfg.machine, "online": True}
        return [agents[k] for k in sorted(agents)]

    def find_stack(self, name: str) -> tuple[dict, dict] | None:
        """(stack, agent) for the online agent that has the stack; offline ones only as a fallback."""
        found = None
        for a in self.agents():
            for s in a.get("stacks", []):
                if s["name"] == name:
                    if a.get("online"):
                        return s, a
                    found = found or (s, a)
        return found

    def agent(self, machine: str) -> dict | None:
        return next((a for a in self.agents() if a["machine"] == machine), None)

    def warnings(self) -> list[tuple[str, str, str]]:
        out = []
        agents = self.agents()
        offline = [a["machine"] for a in agents if not a.get("online")]
        if offline:
            out.append(("warning", f"{len(offline)} agent{'s' if len(offline) > 1 else ''} offline",
                        ", ".join(offline) + " stopped reporting. Its stacks aren't being backed up."))
        lease = self.share.lease()
        claimants = [a["machine"] for a in agents if (a.get("maintenance") or {}).get("wants")]
        if not lease:
            out.append(("warning", "No maintenance agent", "Nothing prunes or checks the repository. "
                                                          "Assign one on the Repository page."))
        elif self.share.lease_stale_now(lease):
            out.append(("warning", "Maintenance agent stopped renewing",
                        f"{lease.machine} hasn't renewed the lease for over 8 days."))
        others = [m for m in claimants if not lease or m != lease.machine]
        if others:
            out.append(("warning", "MAINTENANCE=true is ignored", f"{', '.join(others)} also set MAINTENANCE=true, but "
                        f"{lease.machine if lease else 'no agent'} holds the lease."))
        safe = [f for a in agents for f in a.get("prerestore") or [] if f.get("removable")]
        if safe:
            names = sorted({f["stack"] for f in safe})
            out.append(("warning", f"{len(safe)} set-aside folder{'s' if len(safe) > 1 else ''} can be removed",
                        f"{', '.join(names)}: the restore worked and the stack is healthy. Remove the old folder "
                        "on the stack's page."))
        failed = [s["name"] for a in agents for s in a.get("stacks", []) if (s.get("last") or {}).get("state") == "failed"]
        if failed:
            out.insert(0, ("error", f"{len(failed)} stack{'s' if len(failed) > 1 else ''} failed the last backup",
                           ", ".join(sorted(failed))))
        return out

    def snapshots(self, stack: str | None = None) -> list[dict]:
        now = time.time()
        if not self._snap_cache or now - self._snap_cache[0] > 60:
            snaps = self.service.agent.restic.snapshots()
            self._snap_cache = (now, snaps)
        snaps = self._snap_cache[1]
        return [s.to_dict() for s in snaps if stack is None or s.host == stack]

    def snapshot_count(self) -> tuple[int | None, int | None]:
        try:
            snaps = self.snapshots()
        except ResticError:
            return None, None
        return len(snaps), len({s["stack"] for s in snaps})

    def invalidate_snapshots(self) -> None:
        self._snap_cache = None

    # -- calling agents -------------------------------------------------------------------------------------
    def call(self, machine: str, method: str, path: str, body: dict | None = None) -> dict:
        if machine == self.cfg.machine:
            from backtrack.server import local_agent_call
            return local_agent_call(self.service, method, path, body)
        agent = self.agent(machine)
        if not agent or not agent.get("url"):
            raise AgentUnreachable(f"{machine} has no AGENT_URL in its presence file")
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Content-Type": "application/json", "User-Agent": "backtrack-hub",
                   **sign(self.cluster_key, method, path, data)}
        req = urllib.request.Request(agent["url"].rstrip("/") + path, data=data if body is not None else None,
                                     method=method, headers=headers)
        try:
            with self._open(req, timeout=15) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise AgentUnreachable(f"{machine}: {msg}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AgentUnreachable(f"{machine}: {e}") from e

    def backup(self, stack: str, trigger: str) -> tuple[str, dict]:
        found = self.find_stack(stack)
        if not found:
            raise AgentUnreachable(f"no agent reports a stack named {stack}")
        _, a = found
        return a["machine"], self.call(a["machine"], "POST", f"/v1/stacks/{stack}/backup", {"trigger": trigger})

    def restore(self, stack: str, snapshot: str) -> tuple[str, dict]:
        found = self.find_stack(stack)
        if not found:
            raise AgentUnreachable(f"no agent reports a stack named {stack}")
        _, a = found
        self.invalidate_snapshots()
        return a["machine"], self.call(a["machine"], "POST", f"/v1/stacks/{stack}/restore", {"snapshot": snapshot})

    def prerestore(self, stack: str | None = None) -> list[dict]:
        """Set-aside folders across the fleet, from each agent's presence (the hub's own is live)."""
        out = []
        for a in self.agents():
            for f in a.get("prerestore") or []:
                if stack is None or f.get("stack") == stack:
                    out.append({**f, "machine": a["machine"], "agent_online": bool(a.get("online"))})
        return out

    def remove_prerestore(self, stack: str, path: str, machine: str | None = None) -> tuple[str, dict]:
        """Ask the agent that set the folder aside to remove it. It checks the stack's health again first."""
        matches = [f for f in self.prerestore(stack) if f["path"] == path and (machine is None or f["machine"] == machine)]
        if not matches:
            raise LookupError(f"no agent reports a set-aside folder {path} for {stack}")
        if len({f["machine"] for f in matches}) > 1:
            raise LookupError(f"several agents have {path}; say which machine")
        m = matches[0]["machine"]
        return m, self.call(m, "POST", f"/v1/stacks/{stack}/prerestore/remove", {"path": path})

    def jobs(self) -> tuple[list[tuple[str, dict]], list[str]]:
        out, unreachable = [], []
        for a in self.agents():
            if not a.get("online"):
                continue
            try:
                for j in self.call(a["machine"], "GET", "/v1/jobs").get("jobs", []):
                    out.append((a["machine"], j))
            except AgentUnreachable:
                unreachable.append(a["machine"])
        out.sort(key=lambda p: p[1].get("created") or 0, reverse=True)
        return out[:100], unreachable

    def assign_lease(self, machine: str) -> None:
        if not any(a["machine"] == machine for a in self.agents()):
            raise AgentUnreachable(f"unknown agent {machine}")
        self.share.assign_lease(machine, f"hub:{self.cfg.machine}")
