"""The long-running agent: job runner, scheduler and presence loop."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from backtrack.config import Config
from backtrack.docker import Docker
from backtrack.jobs import Job, JobRunner
from backtrack.notify import Notifier
from backtrack.ops import Agent
from backtrack.restic import Restic
from backtrack.schedule import next_run
from backtrack.share import Share

log = logging.getLogger("backtrack")


def build_agent(cfg: Config) -> Agent:
    docker = Docker(cfg.get("DOCKER_SOCKET"), cfg.get("HELPER_IMAGE"))
    restic = Restic(cfg.get("RESTIC_REPOSITORY"), cfg.get("RESTIC_PASSWORD"), cfg.get("RESTIC_BINARY"),
                    cache_dir=str(cfg.scratch / "restic-cache"))
    share = Share(cfg.share_dir, cfg.machine, float(cfg.get("PRESENCE_STALE_SECONDS")),
                  float(cfg.get("LEASE_STALE_DAYS")))
    notifier = Notifier(cfg.machine, cfg.get("DISCORD_WEBHOOK_URL"), cfg.get("ALERT_WEBHOOK_URL"),
                        cfg.get("HEARTBEAT_URL"))
    return Agent(cfg, docker, restic, share, notifier)


class Service:
    SCHEDULES = (("nightly", "BACKUP_SCHEDULE", "daily"), ("prune", "PRUNE_SCHEDULE", "weekly"),
                 ("monthly", "VERIFY_SCHEDULE", "monthly"))

    def __init__(self, cfg: Config, agent: Agent):
        self.cfg = cfg
        self.agent = agent
        self.runner = JobRunner(cfg.state_dir)
        self._stop = threading.Event()
        self.next_runs: dict[str, datetime] = {}

    # -- jobs -----------------------------------------------------------------------------------------------
    def submit(self, kind: str, stack: str | None = None, **params) -> Job:
        a = self.agent
        fns = {
            "backup": lambda j: a.backup(stack, j, params.get("trigger", "manual")),
            "restore": lambda j: a.restore(stack, j, params.get("snapshot", "latest")),
            "nightly": a.nightly,
            "prune": a.prune,
            "monthly": a.monthly,
            "restore-test": lambda j: a.restore_test(j, stack),
        }
        return self.runner.submit(kind, stack, fns[kind], params)

    def busy_with(self, kind: str) -> bool:
        cur = self.runner.current
        return bool((cur and cur.kind == kind) or any(j.kind == kind for j in self.runner.queued()))

    # -- loops ----------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.runner.start()
        self._compute_next()
        threading.Thread(target=self._schedule_loop, name="scheduler", daemon=True).start()
        threading.Thread(target=self._presence_loop, name="presence", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.runner.stop()

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.get("TZ"))

    def _compute_next(self) -> None:
        now = datetime.now(self.tz())
        for name, key, kind in self.SCHEDULES:
            self.next_runs[name] = next_run(self.cfg.get(key), now, self.tz(), kind)

    def _schedule_loop(self) -> None:
        last_specs = {key: self.cfg.get(key) for _, key, _ in self.SCHEDULES} | {"TZ": self.cfg.get("TZ")}
        while not self._stop.wait(20):
            specs = {key: self.cfg.get(key) for _, key, _ in self.SCHEDULES} | {"TZ": self.cfg.get("TZ")}
            if specs != last_specs:  # a setting changed in the hub UI
                last_specs = specs
                self._compute_next()
            now = datetime.now(self.tz())
            for name, key, kind in self.SCHEDULES:
                when = self.next_runs.get(name)
                if when and now >= when:
                    self.next_runs[name] = next_run(self.cfg.get(key), now, self.tz(), kind)
                    if self.busy_with(name):
                        log.warning("%s is still queued or running; skipping this run", name)
                        continue
                    log.info("scheduled %s", name)
                    self.submit(name)

    def _presence_loop(self) -> None:
        last_lease_check = 0.0
        while True:
            try:
                if time.time() - last_lease_check > 6 * 3600:
                    self.agent.holds_maintenance()  # renews (or claims) the lease
                    last_lease_check = time.time()
                self.agent.share.write_presence(self.presence())
            except Exception as e:  # noqa: BLE001 - the share being away must not kill the agent
                log.warning("presence update failed: %s", e)
            if self._stop.wait(60):
                return

    def presence(self) -> dict:
        data = self.agent.presence(self.runner.current, self.runner.last_results(),
                                   self.runner.last_of("nightly"))
        data["next_runs"] = {k: v.isoformat() for k, v in self.next_runs.items()}
        data["queued"] = [j.to_dict() for j in self.runner.queued()]
        return data

    def status(self) -> dict:
        return {"machine": self.cfg.machine, **self.presence()}

