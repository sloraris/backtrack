"""Thin wrapper around the restic binary."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class ResticError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr

    @property
    def tail(self) -> str:
        return "\n".join(self.stderr.strip().splitlines()[-15:])


@dataclass
class Snapshot:
    id: str
    short_id: str
    time: datetime
    host: str
    paths: list[str]
    tags: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    parent: str | None = None

    def tag(self, name: str) -> str | None:
        prefix = name + "="
        for t in self.tags:
            if t.startswith(prefix):
                return t[len(prefix):]
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "short_id": self.short_id, "time": self.time.isoformat(), "stack": self.host,
            "machine": self.tag("machine"), "trigger": self.tag("trigger"), "paths": self.paths,
            "data_added": self.summary.get("data_added"), "total_bytes": self.summary.get("total_bytes_processed"),
            "files": self.summary.get("total_files_processed"),
        }


def _parse_time(s: str) -> datetime:
    # restic prints RFC3339 with nanoseconds; fromisoformat handles up to microseconds.
    if "." in s:
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Restic:
    def __init__(self, repository: str, password: str, binary: str = "restic", retry_lock: str = "2h",
                 cache_dir: str | None = None):
        self.repository = repository
        self.password = password
        self.binary = binary
        self.retry_lock = retry_lock
        self.cache_dir = cache_dir

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        env["RESTIC_REPOSITORY"] = self.repository
        env["RESTIC_PASSWORD"] = self.password
        if self.cache_dir:
            env["RESTIC_CACHE_DIR"] = self.cache_dir
        return env

    def run(self, *args: str, lock: bool = True, timeout: float | None = None) -> str:
        cmd = [self.binary, *args]
        if lock and self.retry_lock:
            cmd += ["--retry-lock", self.retry_lock]
        try:
            proc = subprocess.run(cmd, env=self._env(), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise ResticError(f"restic binary not found: {self.binary}") from e
        except subprocess.TimeoutExpired as e:
            raise ResticError(f"restic {args[0]} timed out", str(e.stderr or "")) from e
        if proc.returncode != 0:
            # Exit code 3 on backup means "some files could not be read": the snapshot exists but is incomplete.
            raise ResticError(f"restic {args[0]} failed (exit {proc.returncode})", proc.stderr)
        return proc.stdout

    # -- repo -----------------------------------------------------------------------------------------------
    def is_initialized(self) -> bool:
        try:
            self.run("cat", "config", lock=False, timeout=120)
            return True
        except ResticError:
            return False

    def init(self) -> None:
        self.run("init", lock=False)

    # -- snapshots ------------------------------------------------------------------------------------------
    def backup(self, path: Path, host: str, tags: list[str], exclude_file: Path | None = None) -> Snapshot:
        args = ["backup", str(path), "--host", host, "--json", "--exclude-caches", "--one-file-system"]
        for t in tags:
            args += ["--tag", t]
        if exclude_file and exclude_file.is_file():
            args += ["--exclude-file", str(exclude_file)]
        out = self.run(*args)
        snap_id = None
        for line in out.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("message_type") == "summary":
                snap_id = msg.get("snapshot_id")
        if not snap_id:
            raise ResticError("restic backup gave no snapshot id", out[-2000:])
        return self.snapshot(snap_id)

    def snapshots(self, host: str | None = None) -> list[Snapshot]:
        args = ["snapshots", "--json"]
        if host:
            args += ["--host", host]
        data = json.loads(self.run(*args, lock=False) or "[]") or []
        snaps = [
            Snapshot(id=s["id"], short_id=s.get("short_id", s["id"][:8]), time=_parse_time(s["time"]),
                     host=s.get("hostname", ""), paths=s.get("paths", []), tags=s.get("tags") or [],
                     summary=s.get("summary") or {}, parent=s.get("parent"))
            for s in data
        ]
        snaps.sort(key=lambda s: s.time)
        return snaps

    def snapshot(self, snap_id: str) -> Snapshot:
        for s in self.snapshots():
            if s.id == snap_id or s.short_id == snap_id or s.id.startswith(snap_id):
                return s
        raise ResticError(f"snapshot {snap_id} not found")

    def latest(self, host: str) -> Snapshot | None:
        snaps = self.snapshots(host)
        return snaps[-1] if snaps else None

    def restore(self, snapshot: Snapshot, target: Path) -> None:
        """Restore the snapshot's (single) backed-up folder so its contents land in `target`."""
        if len(snapshot.paths) != 1:
            raise ResticError(f"snapshot {snapshot.short_id} has {len(snapshot.paths)} paths, expected 1")
        self.run("restore", f"{snapshot.id}:{snapshot.paths[0]}", "--target", str(target))

    def forget(self, host: str, keep: dict[str, int]) -> None:
        args = ["forget", "--host", host, "--group-by", "host"]
        for k, v in keep.items():
            if v > 0:
                args += [f"--keep-{k}", str(v)]
        if len(args) == 5:
            return  # an all-zero policy would delete everything; never do that implicitly
        self.run(*args)

    def forget_ids(self, ids: list[str]) -> None:
        if ids:
            self.run("forget", *ids)

    def prune(self) -> str:
        return self.run("prune")

    def check(self, subset: str | None = None) -> str:
        args = ["check"]
        if subset:
            args += [f"--read-data-subset={subset}"]
        return self.run(*args)

    def ls_files(self, snapshot: Snapshot) -> tuple[int, int]:
        """(file count, total size) of regular files in the snapshot."""
        out = self.run("ls", "--json", snapshot.id, lock=False)
        count = size = 0
        for line in out.splitlines():
            try:
                node = json.loads(line)
            except ValueError:
                continue
            if node.get("struct_type") == "node" and node.get("type") == "file":
                count += 1
                size += int(node.get("size") or 0)
        return count, size
