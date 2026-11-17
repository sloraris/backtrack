"""Folders a restore set aside (`<stack>.pre-restore`), and when they are safe to remove.

A restore in place renames the stack's folder to `<folder>.pre-restore` (an older one becomes
`<folder>.pre-restore.<timestamp>`). Each one is recorded here, in STATE_DIR/prerestore.json, so the hub can
list them and offer to remove them once the restored stack has proved itself. Backtrack removes only folders it
recorded, and only when asked (or after PRERESTORE_AUTO_REMOVE, when that is set).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backtrack.docker import Container, DockerClient, DockerError

STATE_FILE = "prerestore.json"
_SUFFIX = r"\.pre-restore(\.\d{8}T\d{6})?"
_DURATION = re.compile(r"^(\d+)\s*(s|m|min|h|d|w)?$")
_UNITS = {None: 1, "s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(spec: str) -> int:
    """'300', '5m', '12h', '7d', '2w' -> seconds. '' and '0' mean off (0)."""
    spec = (spec or "").strip().lower()
    if not spec:
        return 0
    m = _DURATION.match(spec)
    if not m:
        raise ValueError(f"invalid duration {spec!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def is_aside_of(path: Path, target: Path) -> bool:
    """True when `path` is a folder name Backtrack itself gives `target` when setting it aside."""
    return path.parent == target.parent and re.fullmatch(re.escape(target.name) + _SUFFIX, path.name) is not None


def folder_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files + dirs:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


@dataclass
class Aside:
    stack: str
    path: str  # the set-aside folder, e.g. /opt/appdata/app.pre-restore
    target: str  # the folder it came from, e.g. /opt/appdata/app
    created: float
    job: str | None = None  # the restore job that set it aside
    snapshot: str | None = None  # what was restored in its place
    safety_snapshot: str | None = None  # the rollback once this folder is gone; None when the folder was empty
    restore_ok: bool = False  # the files came back and post-restore ran; False keeps it for a manual rollback
    restored_at: float | None = None
    size: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class Registry:
    """The agent's list of set-aside folders. Thread-safe; written atomically."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / STATE_FILE
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path.exists()

    def _read(self) -> list[Aside]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        out = []
        for d in data.get("folders", []):
            try:
                out.append(Aside(**{k: v for k, v in d.items() if k in Aside.__dataclass_fields__}))
            except TypeError:
                continue
        return out

    def _write(self, items: list[Aside]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"folders": [a.to_dict() for a in items]}, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    def all(self) -> list[Aside]:
        """Recorded folders that still exist; records of folders removed by hand are dropped."""
        with self._lock:
            items = self._read()
            alive = [a for a in items if os.path.lexists(a.path)]
            if len(alive) != len(items):
                self._write(alive)
            return sorted(alive, key=lambda a: (a.stack, -a.created))

    def get(self, path: str) -> Aside | None:
        return next((a for a in self.all() if a.path == path), None)

    def add(self, aside: Aside) -> None:
        with self._lock:
            items = [a for a in self._read() if a.path != aside.path]
            items.append(aside)
            self._write(items)

    def update(self, key: str, /, **changes) -> None:
        with self._lock:
            items = self._read()
            for a in items:
                if a.path == key:
                    for k, v in changes.items():
                        setattr(a, k, v)
            self._write(items)

    def moved(self, old: str, new: str) -> None:
        """A rotation renamed `old` to `new`."""
        self.update(old, path=new)

    def remove(self, path: str) -> None:
        with self._lock:
            self._write([a for a in self._read() if a.path != path])

    def adopt_history(self, jobs: list[dict]) -> int:
        """First run after an upgrade: record folders that earlier restores set aside, from the job history."""
        with self._lock:
            if self.path.exists():
                return 0
            found: dict[str, Aside] = {}
            for j in sorted(jobs, key=lambda j: j.get("finished") or 0):
                res = j.get("result") or {}
                path = res.get("set_aside")
                if j.get("kind") != "restore" or not j.get("stack") or not path:
                    continue
                target = str(Path(path).with_name(Path(path).name[:-len(".pre-restore")])) \
                    if path.endswith(".pre-restore") else None
                if not target:
                    continue
                found[path] = Aside(stack=j["stack"], path=path, target=target,
                                    created=j.get("started") or j.get("created") or time.time(), job=j.get("id"),
                                    snapshot=res.get("snapshot"), safety_snapshot=res.get("safety_snapshot"),
                                    restore_ok=j.get("state") == "ok", restored_at=j.get("finished"))
            items = [a for a in found.values() if os.path.isdir(a.path) and not os.path.islink(a.path)]
            for a in items:
                a.size = folder_size(Path(a.path))
            self._write(items)
            return len(items)


@dataclass
class Verdict:
    """Whether a set-aside folder may go, and why (or why not), in words for the UI."""
    state: str  # settling, safe, unhealthy, failed
    reason: str
    removable: bool = False
    containers: list[dict] = field(default_factory=list)


def judge(aside: Aside, containers: list[Container] | None, docker: DockerClient, settle: int,
          now: float | None = None) -> Verdict:
    """Is the stack that replaced this folder healthy?

    Healthy: the restore succeeded, every container of the stack is running, those with a healthcheck report
    `healthy`, and each has stayed up for `settle` seconds since its last start (a restart resets the clock) and
    since the restore. A stack with no containers on this host (a files-only restore) only needs the settle time.
    """
    now = time.time() if now is None else now
    if not aside.restore_ok:
        return Verdict("failed", "The restore did not finish. Keep this folder to roll back by hand.")
    since_restore = now - (aside.restored_at or aside.created)
    if not containers:
        if since_restore < settle:
            return Verdict("settling", "Restored moments ago; waiting before offering removal.")
        return Verdict("safe", "No containers to check on this host; the restore finished.", True)
    rows, problems, waiting = [], [], False
    for c in containers:
        try:
            h = docker.inspect(c.id)
        except DockerError as e:
            problems.append(f"{c.name}: {e}")
            continue
        up_for = now - h.started_at if h.started_at else 0
        rows.append({"name": c.name, "running": h.running, "health": h.health, "up_for": int(up_for)})
        if not h.running:
            problems.append(f"{c.name} is not running")
        elif h.health == "unhealthy":
            problems.append(f"{c.name} is unhealthy")
        elif h.health == "starting" or up_for < settle:
            waiting = True
    if problems:
        return Verdict("unhealthy", "; ".join(problems) + ".", False, rows)
    if waiting or since_restore < settle:
        return Verdict("settling", f"Waiting for the stack to stay healthy for {settle // 60 or settle}"
                       f"{' min' if settle >= 60 else ' s'}.", False, rows)
    return Verdict("safe", "The stack has been running and healthy since the restore.", True, rows)
