"""What an agent does: back up, restore, auto-restore, restore test, prune and check."""

from __future__ import annotations

import os
import random
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from backtrack import __version__
from backtrack.config import Config
from backtrack.docker import DockerClient, DockerError
from backtrack.jobs import Job, exclusive
from backtrack.notify import Notifier, call_hooks
from backtrack.restic import Restic, ResticError, Snapshot
from backtrack.share import Share
from backtrack.stacks import IGNORE_FILE, NAME_RE, Stack, discover, has_files, literal_ignores


class OpError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class Agent:
    def __init__(self, cfg: Config, docker: DockerClient, restic: Restic, share: Share, notifier: Notifier):
        self.cfg = cfg
        self.docker = docker
        self.restic = restic
        self.share = share
        self.notifier = notifier

    # -- discovery ------------------------------------------------------------------------------------------
    def stacks(self) -> list[Stack]:
        return discover(self.docker.containers(), self.cfg.appdata, self.cfg.keep)

    def find(self, name: str) -> Stack | None:
        for s in self.stacks():
            if s.name == name:
                return s
        return None

    def _require(self, name: str) -> Stack:
        stack = self.find(name)
        if stack is None:
            raise OpError(f"no stack {name!r} with a backtrack.mode label on {self.cfg.machine}")
        if stack.error:
            raise OpError(f"{name}: {stack.error}")
        return stack

    # -- stop / start ---------------------------------------------------------------------------------------
    def _hooks(self, stack: Stack, event: str) -> list[str | None]:
        if event == "pre-stop":
            return [stack.pre_stop, self.cfg.get("HOOK_PRE_STOP_URL")]
        return [stack.post_start, self.cfg.get("HOOK_POST_START_URL")]

    def _hook_payload(self, stack: Stack, event: str, trigger: str, snapshot: str | None = None,
                      ok: bool = True) -> dict:
        return {"event": event, "stack": stack.name, "machine": self.cfg.machine, "trigger": trigger,
                "snapshot": snapshot, "ok": ok}

    def _stop(self, stack: Stack, job: Job, trigger: str) -> list[str]:
        """Stop the stack's running containers. Returns the ids that were running."""
        running = [c for c in stack.stop_order() if c.running]
        if not running:
            job.log("stack has no running containers; nothing to stop")
            return []
        call_hooks(self._hooks(stack, "pre-stop"), self._hook_payload(stack, "pre-stop", trigger), job.log)
        timeout = int(self.cfg.get("STOP_TIMEOUT"))
        for c in running:
            job.log(f"stopping {c.name}")
            self.docker.stop(c.id, timeout)
        return [c.id for c in running]

    def _start(self, stack: Stack, ids: list[str], job: Job, trigger: str, snapshot: str | None,
               ok: bool) -> None:
        if not ids:
            return
        errors = []
        for c in stack.start_order():
            if c.id in ids:
                job.log(f"starting {c.name}")
                try:
                    self.docker.start(c.id)
                except DockerError as e:
                    errors.append(f"{c.name}: {e}")
                    job.log(f"ERROR: could not start {c.name}: {e}")
        call_hooks(self._hooks(stack, "post-start"),
                   self._hook_payload(stack, "post-start", trigger, snapshot, ok and not errors), job.log)
        if errors:
            self.notifier.failure(stack.name, "start", "containers did not start again", "\n".join(errors))
            raise OpError("containers did not start again: " + "; ".join(errors))

    @contextmanager
    def _stopped(self, stack: Stack, job: Job, trigger: str):
        """Stop for the duration of the block; start again no matter what happened inside."""
        ids = self._stop(stack, job, trigger) if stack.mode == "stop" else []
        state = {"snapshot": None, "ok": False}
        try:
            yield state
        finally:
            self._start(stack, ids, job, trigger, state["snapshot"], state["ok"])

    # -- backup ---------------------------------------------------------------------------------------------
    def _tags(self, stack: str, trigger: str) -> list[str]:
        return [f"stack={stack}", f"machine={self.cfg.machine}", f"trigger={trigger}"]

    def _snapshot_folder(self, name: str, path: Path, trigger: str, job: Job) -> Snapshot:
        job.log(f"restic backup {path} (stack {name}, trigger {trigger})")
        snap = self.restic.backup(path, host=name, tags=self._tags(name, trigger),
                                  exclude_file=path / IGNORE_FILE)
        added = snap.summary.get("data_added")
        job.log(f"snapshot {snap.short_id} saved" + (f", {added} bytes added" if added is not None else ""))
        return snap

    def _backup_stack(self, stack: Stack, trigger: str, job: Job) -> Snapshot:
        if stack.error:
            raise OpError(stack.error)
        assert stack.path is not None
        if not has_files(stack.path):
            raise OpError(f"{stack.path} is missing or empty; refusing to save an empty snapshot")
        with self._stopped(stack, job, trigger) as state:
            snap = self._snapshot_folder(stack.name, stack.path, trigger, job)
            state["snapshot"], state["ok"] = snap.id, True
        try:
            self.restic.forget(stack.name, stack.keep or self.cfg.keep)
            job.log(f"retention applied: {stack.keep}")
        except ResticError as e:
            # The backup itself worked; a failed forget is reported but doesn't fail the backup.
            job.log(f"WARNING: forget failed: {e}\n{e.tail}")
            self.notifier.failure(stack.name, "forget", str(e), e.tail)
        return snap

    def backup(self, name: str, job: Job, trigger: str = "manual") -> dict:
        if trigger not in ("manual", "pre-migrate", "nightly"):
            raise OpError(f"unknown trigger {trigger!r}")
        with exclusive(self.cfg.state_dir, job):
            stack = self._require(name)
            try:
                snap = self._backup_stack(stack, trigger, job)
            except (OpError, ResticError, DockerError) as e:
                detail = getattr(e, "tail", "") or getattr(e, "detail", "")
                self.notifier.failure(name, "backup", str(e), detail)
                raise
        return {"snapshot": snap.short_id, "data_added": snap.summary.get("data_added")}

    def nightly(self, job: Job) -> dict:
        results: dict[str, dict] = {}
        with exclusive(self.cfg.state_dir, job):
            stacks = self.stacks()
            job.log(f"{len(stacks)} stacks: {', '.join(s.name for s in stacks) or 'none'}")
            for stack in stacks:
                try:
                    snap = self._backup_stack(stack, "nightly", job)
                    results[stack.name] = {"state": "ok", "snapshot": snap.short_id,
                                           "data_added": snap.summary.get("data_added")}
                except Exception as e:  # noqa: BLE001 - one stack failing must not stop the others
                    detail = getattr(e, "tail", "") or getattr(e, "detail", "")
                    job.log(f"ERROR: {stack.name}: {e}")
                    results[stack.name] = {"state": "failed", "error": str(e)}
                    self.notifier.failure(stack.name, "backup", str(e), detail)
        failed = [n for n, r in results.items() if r["state"] != "ok"]
        self.notifier.summary(failed, len(results))
        self.notifier.heartbeat(not failed, f"{len(results) - len(failed)}/{len(results)} stacks backed up"
                                + (f"; failed: {', '.join(failed)}" if failed else ""))
        return {"stacks": results, "failed": failed}

    # -- restore --------------------------------------------------------------------------------------------
    def _resolve(self, name: str, snapshot: str) -> Snapshot:
        if snapshot in ("", "latest"):
            snap = self.restic.latest(name)
            if snap is None:
                raise OpError(f"no snapshots for {name}")
            return snap
        snap = self.restic.snapshot(snapshot)
        if snap.host != name:
            raise OpError(f"snapshot {snap.short_id} belongs to {snap.host}, not {name}")
        return snap

    def _post_restore(self, name: str, command: str | None, path: Path, snap: Snapshot, job: Job) -> None:
        if not command:
            return
        job.log("running post-restore command on the host")
        code, output = self.docker.run_host_command(command, {
            "BACKTRACK_STACK": name, "BACKTRACK_PATH": str(path), "BACKTRACK_SNAPSHOT": snap.id})
        for line in output.strip().splitlines()[-50:]:
            job.log(f"  | {line}")
        if code != 0:
            raise OpError(f"post-restore command exited {code}", output[-2000:])

    @staticmethod
    def _carry_over(old: Path, new: Path, job: Job) -> None:
        """Move data that was excluded from backups from the old folder into the restored one."""
        for rel in literal_ignores(old / IGNORE_FILE, old):
            src, dst = old / rel, new / rel
            if not (src.exists() or src.is_symlink()) or dst.exists() or dst.is_symlink():
                continue
            parent = dst.parent
            if not parent.exists():
                parent.mkdir(parents=True)
                # match the old parent's owner and mode
                st = src.parent.stat()
                os.chmod(parent, st.st_mode & 0o7777)
                try:
                    os.chown(parent, st.st_uid, st.st_gid)
                except PermissionError:
                    pass
            os.rename(src, dst)
            job.log(f"kept excluded {rel} from the previous folder")

    @staticmethod
    def _set_aside(path: Path, job: Job) -> Path | None:
        if not (path.exists() or path.is_symlink()):
            return None
        aside = path.with_name(path.name + ".pre-restore")
        if aside.exists() or aside.is_symlink():
            older = aside.with_name(aside.name + "." + datetime.now().strftime("%Y%m%dT%H%M%S"))
            os.rename(aside, older)
            job.log(f"renamed the previous {aside.name} to {older.name}")
        os.rename(path, aside)
        job.log(f"renamed {path} to {aside.name}")
        return aside

    def restore(self, name: str, job: Job, snapshot: str = "latest", path: Path | None = None) -> dict:
        """Restore in place: stop, safety snapshot, set the folder aside, restore, carry over, start."""
        with exclusive(self.cfg.state_dir, job):
            stack = self.find(name)
            if stack and stack.error:
                raise OpError(f"{name}: {stack.error}")
            if stack is None:
                if not NAME_RE.match(name):
                    raise OpError(f"invalid stack name {name!r}")
                job.log(f"{name} has no containers here; restoring files only")
                stack = Stack(name=name, mode="live", path=path or self.cfg.appdata / name)
            target = path or stack.path
            assert target is not None
            snap = self._resolve(name, snapshot)
            job.log(f"restoring {name} from snapshot {snap.short_id} ({snap.time:%Y-%m-%d %H:%M}, "
                    f"machine {snap.tag('machine')}) into {target}")

            stop_stack = Stack(**{**stack.__dict__, "mode": "stop"})  # restores always stop the stack
            ids = self._stop(stop_stack, job, "restore")
            safety = None
            try:
                if has_files(target):
                    safety = self._snapshot_folder(name, target, "pre-restore", job)
            except (ResticError, OpError) as e:
                job.log("safety snapshot failed; nothing was changed, starting the stack again")
                self._start(stack, ids, job, "restore", None, False)
                self.notifier.failure(name, "restore", f"safety snapshot failed: {e}", getattr(e, "tail", ""))
                raise
            aside = None
            try:
                aside = self._set_aside(target, job)
                target.parent.mkdir(parents=True, exist_ok=True)
                self.restic.restore(snap, target)
                job.log("files restored")
                if aside:
                    self._carry_over(aside, target, job)
                self._post_restore(name, stack.post_restore, target, snap, job)
            except Exception as e:
                hint = (f"The stack is left stopped. To roll back: remove {target} and rename {aside} to "
                        f"{target.name}, then start the stack." if aside else "The stack is left stopped.")
                job.log(f"ERROR: {e}. {hint}")
                self.notifier.failure(name, "restore", f"{e}. {hint}", getattr(e, "tail", "") or
                                      getattr(e, "detail", ""))
                raise
            start_ids = ids or [c.id for c in stack.containers]
            self._start(stack, start_ids, job, "restore", snap.id, True)
        return {"snapshot": snap.short_id, "safety_snapshot": safety.short_id if safety else None,
                "set_aside": str(aside) if aside else None}

    def auto_restore(self, name: str, job: Job, path: Path | None = None) -> int:
        """Pre-deploy hook: restore latest when the folder is missing or empty. Returns an exit code."""
        if not NAME_RE.match(name):
            job.log(f"invalid stack name {name!r}")
            return 2
        target = path or self.cfg.appdata / name
        if has_files(target):
            job.log(f"{target} has data; nothing to restore")
            return 0
        with exclusive(self.cfg.state_dir, job):
            if has_files(target):
                job.log(f"{target} has data; nothing to restore")
                return 0
            elsewhere = self.share.running_elsewhere(name)
            if elsewhere:
                job.log(f"REFUSING: {name} is still running on {', '.join(elsewhere)}. Stop it there first, "
                        "so the stack never runs on two hosts with diverging data.")
                self.notifier.failure(name, "auto-restore", f"refused: still running on {', '.join(elsewhere)}")
                return 1
            try:
                snap = self.restic.latest(name)
            except ResticError as e:
                job.log(f"ERROR: cannot read the repository: {e}\n{e.tail}")
                self.notifier.failure(name, "auto-restore", f"cannot read the repository: {e}", e.tail)
                return 1
            if snap is None:
                job.log(f"no snapshot for {name}; it starts fresh")
                return 0
            job.log(f"{target} is missing or empty; restoring snapshot {snap.short_id} "
                    f"({snap.time:%Y-%m-%d %H:%M}, machine {snap.tag('machine')})")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self.restic.restore(snap, target)
                stack = self.find(name)
                self._post_restore(name, stack.post_restore if stack else None, target, snap, job)
            except (ResticError, OpError, DockerError, OSError) as e:
                detail = getattr(e, "tail", "") or getattr(e, "detail", "")
                job.log(f"ERROR: auto-restore failed: {e}\n{detail}")
                self.notifier.failure(name, "auto-restore", str(e), detail)
                return 1
            job.log("restored")
            return 0

    # -- verification and maintenance -----------------------------------------------------------------------
    def restore_test(self, job: Job, stack_name: str | None = None) -> dict:
        with exclusive(self.cfg.state_dir, job):
            candidates = [s for s in self.stacks() if not s.error]
            if stack_name:
                candidates = [s for s in candidates if s.name == stack_name]
            random.shuffle(candidates)
            snap = stack = None
            for s in candidates:
                snap = self.restic.latest(s.name)
                if snap:
                    stack = s
                    break
            if not stack or not snap:
                job.log("no stack with snapshots to test")
                return {}
            scratch = self.cfg.scratch / "restore-test" / stack.name
            job.log(f"restore test: {stack.name} snapshot {snap.short_id} -> {scratch}")
            try:
                if scratch.exists():
                    shutil.rmtree(scratch)
                scratch.mkdir(parents=True)
                self.restic.restore(snap, scratch)
                want_files, want_size = self.restic.ls_files(snap)
                got_files = got_size = 0
                for root, _dirs, files in os.walk(scratch):
                    for f in files:
                        p = os.path.join(root, f)
                        if os.path.isfile(p) and not os.path.islink(p):
                            got_files += 1
                            got_size += os.path.getsize(p)
                job.log(f"snapshot: {want_files} files, {want_size} bytes; restored: {got_files} files, "
                        f"{got_size} bytes")
                if (got_files, got_size) != (want_files, want_size):
                    raise OpError("restored files do not match the snapshot")
            except (ResticError, OpError, OSError) as e:
                self.notifier.failure(stack.name, "restore test", str(e), getattr(e, "tail", ""))
                raise
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        return {"stack": stack.name, "snapshot": snap.short_id, "files": got_files}

    def holds_maintenance(self) -> bool:
        return self.share.holds_maintenance(self.cfg.maintenance)

    def prune(self, job: Job, force: bool = False) -> dict:
        if not force and not self.holds_maintenance():
            job.log("not the maintenance agent; skipping prune")
            return {"skipped": True}
        with exclusive(self.cfg.state_dir, job):
            job.log("restic prune (locks the repository)")
            try:
                out = self.restic.prune()
            except ResticError as e:
                self.notifier.failure(None, "prune", str(e), e.tail)
                raise
            for line in out.strip().splitlines()[-10:]:
                job.log(f"  | {line}")
        return {"pruned": True}

    def check(self, job: Job, force: bool = False, subset: str | None = None) -> dict:
        if not force and not self.holds_maintenance():
            job.log("not the maintenance agent; skipping repository check")
            return {"skipped": True}
        subset = subset or f"{datetime.now().month}/12"
        with exclusive(self.cfg.state_dir, job):
            job.log(f"restic check --read-data-subset={subset}")
            try:
                self.restic.check(subset)
            except ResticError as e:
                self.notifier.failure(None, "repository check", str(e), e.tail)
                raise
            job.log("repository check passed")
        return {"checked": subset}

    def monthly(self, job: Job) -> dict:
        result: dict = {}
        errors = []
        for name, fn in (("restore_test", self.restore_test), ("check", self.check)):
            try:
                result[name] = fn(job)
            except Exception as e:  # noqa: BLE001 - run both, report both
                errors.append(f"{name}: {e}")
                job.log(f"ERROR: {name}: {e}")
        if errors:
            result["failed"] = errors
        return result

    def forget_stack(self, name: str, job: Job) -> dict:
        with exclusive(self.cfg.state_dir, job):
            ids = [s.id for s in self.restic.snapshots(name)]
            job.log(f"forgetting {len(ids)} snapshots of {name}")
            self.restic.forget_ids(ids)
        return {"forgotten": len(ids)}

    # -- presence -------------------------------------------------------------------------------------------
    def presence(self, current: Job | None = None, last: dict | None = None,
                 last_nightly: Job | None = None) -> dict:
        try:
            stacks = self.stacks()
            stack_list = [s.to_dict() for s in stacks]
            docker_error = None
        except DockerError as e:
            stack_list, docker_error = [], str(e)
        last = last or {}
        for s in stack_list:
            s["last"] = last.get(s["name"])
        lease = self.share.lease()
        return {
            "version": __version__, "url": self.cfg.get("AGENT_URL"), "hub": self.cfg.hub,
            "stacks": stack_list, "docker_error": docker_error,
            "job": current.to_dict() if current else None,
            "last_nightly": last_nightly.to_dict() if last_nightly else None,
            "maintenance": {"wants": self.cfg.maintenance, "locked": self.cfg.locked("MAINTENANCE"),
                            "holds": bool(lease and lease.machine == self.cfg.machine)},
            "time": time.time(),
        }
