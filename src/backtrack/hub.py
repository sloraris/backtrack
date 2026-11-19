"""Hub side: reads the fleet from presence files and calls agents' signed APIs."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable

from backtrack.restic import ResticError
from backtrack.signing import sign


# The first release whose agents answer POST /v1/backup-all.
BACKUP_ALL_SINCE = (0, 3, 0)
# The first release whose agents answer POST /v1/bulk/... An older agent must not get a stack list it would ignore.
BULK_SINCE = (0, 4, 0)


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

    def find_stack(self, name: str, machine: str | None = None) -> tuple[dict, dict] | None:
        """(stack, agent) for the agent that has the stack, or the one on `machine`.

        Preferred: an online agent where the stack has containers; then an online agent that only remembers it
        (state `absent`, after `compose down` or a move); offline agents last.
        """
        found = []
        for a in self.agents():
            if machine and a["machine"] != machine:
                continue
            for s in a.get("stacks", []):
                if s["name"] == name:
                    found.append((not a.get("online"), s.get("state") == "absent", a["machine"], s, a))
        if not found:
            return None
        best = min(found, key=lambda f: f[:3])
        return best[3], best[4]

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

    def backup_all(self, machine: str | None = None) -> dict:
        """Queue a backup-all job on every online agent that has stacks (or on `machine` only).

        Agents run in parallel; each backs up its own stacks one at a time. Agents that are offline, unreachable,
        too old, without stacks, or already running one are skipped and listed with the reason.
        """
        agents = self.agents()
        if machine:
            agents = [a for a in agents if a["machine"] == machine]
            if not agents:
                raise LookupError(f"no agent {machine}")
        started, skipped = [], []
        for a in agents:
            m = a["machine"]
            names = [s["name"] for s in a.get("stacks", []) if s.get("state") != "absent"]  # agents skip those
            reason = None
            if not a.get("online"):
                reason = "offline"
            elif not names:
                reason = "no stacks"
            elif older_than(a.get("version"), BACKUP_ALL_SINCE):
                reason = f"runs {a.get('version')}; Back up all needs 0.3.0 or later"
            if reason:
                skipped.append({"machine": m, "reason": reason})
                continue
            try:
                res = self.call(m, "POST", "/v1/backup-all", {})
            except AgentUnreachable as e:
                skipped.append({"machine": m, "reason": str(e)})
                continue
            if res.get("existing"):
                skipped.append({"machine": m, "reason": "a backup-all is already queued or running",
                                "job": res["job"]})
            else:
                started.append({"machine": m, "job": res["job"], "stacks": names})
        return {"started": started, "skipped": skipped}

    # -- bulk actions on selected stacks ---------------------------------------------------------------------
    BULK_ACTIONS = ("backup", "prerestore-remove")

    def bulk_plan(self, action: str, stacks: list[str]) -> dict:
        """Which agent gets which of the selected stacks, and which stacks are skipped and why. Calls no agent.

        backup: each stack goes to the online agent that reports it. prerestore-remove: each stack goes to the
        agents that hold a set-aside folder of it that is safe to remove. Stacks no agent can take (unknown,
        nothing to remove, agent offline or older than 0.4.0) are skipped with the reason.
        """
        if action not in self.BULK_ACTIONS:
            raise LookupError(f"no bulk action {action}")
        groups: dict[str, list[str]] = {}
        skipped: list[dict] = []
        folders = self.prerestore() if action == "prerestore-remove" else []
        for name in stacks:
            if action == "backup":
                found = self.find_stack(name)
                if not found:
                    skipped.append({"stack": name, "reason": "no agent reports it"})
                    continue
                machines = [found[1]["machine"]]
            else:
                mine = [f for f in folders if f["stack"] == name]
                machines = sorted({f["machine"] for f in mine if f.get("removable")})
                if not machines:
                    skipped.append({"stack": name, "reason": "no set-aside folders" if not mine
                                    else "no set-aside folder is safe to remove yet"})
                    continue
            for m in machines:
                groups.setdefault(m, []).append(name)
        ready: dict[str, list[str]] = {}
        for m, names in sorted(groups.items()):
            reason = self._bulk_blocker(m)
            if reason:
                skipped += [{"stack": n, "machine": m, "reason": reason} for n in names]
            else:
                ready[m] = names
        return {"ready": ready, "skipped": skipped}

    def bulk(self, action: str, stacks: list[str]) -> dict:
        """Run a bulk action: one job per agent with its share of the stacks, agents in parallel."""
        plan = self.bulk_plan(action, stacks)
        started, skipped = [], plan["skipped"]
        for m, names in plan["ready"].items():
            try:
                res = self.call(m, "POST", f"/v1/bulk/{action}", {"stacks": names})
            except AgentUnreachable as e:
                skipped += [{"stack": n, "machine": m, "reason": str(e)} for n in names]
                continue
            started.append({"machine": m, "job": res["job"], "stacks": names,
                            **({"existing": True} if res.get("existing") else {})})
        return {"started": started, "skipped": skipped}

    def _bulk_blocker(self, machine: str) -> str | None:
        a = self.agent(machine) or {}
        if not a.get("online"):
            return "agent offline"
        if older_than(a.get("version"), BULK_SINCE):
            return f"{machine} runs {a.get('version')}; bulk actions need 0.4.0 or later"
        return None

    def untrack(self, stack: str, machine: str) -> dict:
        """Ask `machine` to stop listing a stack whose containers are gone from it. Snapshots stay."""
        found = self.find_stack(stack, machine)
        if not found:
            raise LookupError(f"{machine} doesn't report a stack named {stack}")
        if found[0].get("state") != "absent":
            raise LookupError(f"{stack} still has containers on {machine}")
        return self.call(machine, "POST", f"/v1/stacks/{stack}/untrack", {})

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

    def jobs(self, limit: int | None = 100, per_agent: int = 50,
             machine: str | None = None) -> tuple[list[tuple[str, dict]], list[str]]:
        """Recent jobs across online agents, newest first (`limit=None`: all), and the agents that didn't answer."""
        out, unreachable = [], []
        for a in self.agents():
            if not a.get("online") or (machine and a["machine"] != machine):
                continue
            try:
                for j in self.call(a["machine"], "GET", f"/v1/jobs?limit={per_agent}").get("jobs", []):
                    out.append((a["machine"], j))
            except AgentUnreachable:
                unreachable.append(a["machine"])
        out.sort(key=lambda p: p[1].get("created") or 0, reverse=True)
        return out[:limit], unreachable

    def assign_lease(self, machine: str) -> None:
        if not any(a["machine"] == machine for a in self.agents()):
            raise AgentUnreachable(f"unknown agent {machine}")
        self.share.assign_lease(machine, f"hub:{self.cfg.machine}")


def older_than(version: str | None, minimum: tuple[int, ...]) -> bool:
    """True for a release older than `minimum`. Unknown or unparsable versions (a dev build) get the benefit."""
    try:
        return tuple(int(x) for x in (version or "").split(".")[:3]) < minimum
    except ValueError:
        return False
