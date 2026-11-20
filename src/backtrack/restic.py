"""Thin wrapper around the restic binary."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
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
    # The offset is "Z" or "+hh:mm"/"-hh:mm" (restic uses the container's TZ), and must survive the trim.
    m = re.fullmatch(r"([^.]+)(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})", s.strip())
    if not m:
        raise ValueError(f"unexpected snapshot time {s!r}")
    head, frac, tz = m.groups()
    frac = f".{frac[:6]}" if frac else ""
    return datetime.fromisoformat(head + frac + ("+00:00" if tz == "Z" else tz))


# Snapshots Backtrack takes on its own schedule or on request, which the retention policy applies to. A
# "pre-restore" snapshot is a safety copy of the folder taken just before a restore: it is never "latest" while a
# regular snapshot exists, and it has its own retention so it can't take a regular snapshot's daily slot.
REGULAR_TRIGGERS = ("nightly", "manual", "pre-migrate")
SAFETY_TRIGGER = "pre-restore"
SAFETY_KEEP_WITHIN = "30d"

_GO_SETUID, _GO_SETGID, _GO_STICKY = 1 << 23, 1 << 22, 1 << 20


def go_mode_to_unix(mode: int) -> int:
    """restic stores Go os.FileMode: permission bits are the low 9, setuid/setgid/sticky live in high bits."""
    out = mode & 0o777
    if mode & _GO_SETUID:
        out |= 0o4000
    if mode & _GO_SETGID:
        out |= 0o2000
    if mode & _GO_STICKY:
        out |= 0o1000
    return out


def apply_node_metadata(path: Path, node: dict) -> None:
    try:
        os.chown(path, int(node.get("uid", -1)), int(node.get("gid", -1)))
    except PermissionError:
        pass  # not root (tests on a laptop): owners can't be set, modes still can
    if "mode" in node:
        os.chmod(path, go_mode_to_unix(int(node["mode"])))
    if node.get("mtime"):
        t = _parse_time(node["mtime"]).timestamp()
        os.utime(path, (t, t))


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
    def backup(self, path: Path, host: str, tags: list[str], excludes: list[str] | None = None) -> Snapshot:
        args = ["backup", str(path), "--host", host, "--json", "--exclude-caches", "--one-file-system"]
        for t in tags:
            args += ["--tag", t]
        with tempfile.NamedTemporaryFile("w", prefix="backtrack-exclude-", suffix=".txt") as ex:
            if excludes:
                ex.write("\n".join(excludes) + "\n")
                ex.flush()
                args += ["--exclude-file", ex.name]
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
        # restic takes a shared lock for `snapshots` even when asked not to wait, so without --retry-lock this fails
        # (exit 11) while another machine's forget or prune holds the repository.
        data = json.loads(self.run(*args) or "[]") or []
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
        """The newest regular snapshot of a stack, or its newest safety snapshot if that's all there is."""
        snaps = self.snapshots(host)
        regular = [s for s in snaps if s.tag("trigger") != SAFETY_TRIGGER]
        return (regular or snaps or [None])[-1]

    def restore(self, snapshot: Snapshot, target: Path) -> None:
        """Restore the snapshot's (single) backed-up folder so its contents land in `target`."""
        if len(snapshot.paths) != 1:
            raise ResticError(f"snapshot {snapshot.short_id} has {len(snapshot.paths)} paths, expected 1")
        self.run("restore", f"{snapshot.id}:{snapshot.paths[0]}", "--target", str(target))
        # `id:path --target dir` restores the folder's contents but not the folder's own owner and mode,
        # so apply them from the snapshot (a stack folder owned by uid 1000 must come back that way).
        node = self.root_node(snapshot)
        if node:
            apply_node_metadata(target, node)

    def root_node(self, snapshot: Snapshot) -> dict | None:
        """The snapshot's metadata for its backed-up folder itself (`restic ls` prints it first)."""
        root = snapshot.paths[0]
        # --no-lock: this is killed as soon as the first node arrives, and a killed restic leaves its lock behind
        cmd = [self.binary, "ls", "--no-lock", "--json", snapshot.id, root]
        with subprocess.Popen(cmd, env=self._env(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True) as proc:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    try:
                        node = json.loads(line)
                    except ValueError:
                        continue
                    if node.get("struct_type") == "node" and node.get("path") == root:
                        return node
            finally:
                proc.kill()
        return None

    def forget(self, host: str, keep: dict[str, int]) -> None:
        """Apply the retention policy to a stack's regular snapshots, then expire its old safety snapshots."""
        policy = [arg for k, v in keep.items() if v > 0 for arg in (f"--keep-{k}", str(v))]
        if not policy:
            return  # an all-zero policy would delete everything; never do that implicitly
        self.unlock_stale()
        regular = [arg for t in REGULAR_TRIGGERS for arg in ("--tag", f"trigger={t}")]  # repeated --tag means any
        self.run("forget", "--host", host, "--group-by", "host", *regular, *policy)
        self.run("forget", "--host", host, "--group-by", "host", "--tag", f"trigger={SAFETY_TRIGGER}",
                 "--keep-within", SAFETY_KEEP_WITHIN, "--keep-last", "1")

    def unlock_stale(self) -> None:
        """Remove stale locks before an operation that needs the repository to itself.

        restic doesn't skip a lock left by a process that died (a crash, a killed container); it waits for it until
        --retry-lock runs out. `restic unlock` removes only stale locks: from a process that is gone on this machine,
        or not refreshed for 30 minutes. Another agent's live lock is left alone.
        """
        try:
            self.run("unlock", lock=False, timeout=600)
        except ResticError:
            pass  # the operation itself will report a real problem with the repository

    def forget_ids(self, ids: list[str]) -> None:
        if ids:
            self.unlock_stale()
            self.run("forget", *ids)

    def prune(self) -> str:
        self.unlock_stale()
        return self.run("prune")

    def check(self, subset: str | None = None) -> str:
        self.unlock_stale()
        args = ["check"]
        if subset:
            args += [f"--read-data-subset={subset}"]
        return self.run(*args)

    def ls_files(self, snapshot: Snapshot) -> tuple[int, int]:
        """(file count, total size) of regular files in the snapshot."""
        out = self.run("ls", "--json", snapshot.id)
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
