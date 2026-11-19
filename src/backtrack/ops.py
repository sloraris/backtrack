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
from backtrack.prerestore import Aside, Registry, Verdict, folder_size, is_aside_of, judge, parse_duration
from backtrack.restic import Restic, ResticError, Snapshot
from backtrack.share import Share
from backtrack.stacks import (IGNORE_FILE, NAME_RE, KnownStacks, Stack, build_stack, discover, has_files,
                              literal_ignores, literal_matches, restic_excludes)


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
        self.asides = Registry(cfg.state_dir)
        self.known = KnownStacks(cfg.state_dir)
        self.clock = time.time

    # -- discovery ------------------------------------------------------------------------------------------
    def stacks(self) -> list[Stack]:
        """Stacks with containers here, plus remembered ones whose containers are gone (state `absent`)."""
        containers = self.docker.containers()
        stacks = discover(containers, self.cfg.appdata, self.cfg.keep)
        try:
            known = self.known.update(stacks, {c.project for c in containers}, self.clock())
        except OSError:
            known = self.known.load()  # a read-only or full state folder must not hide the running stacks
        present = {s.name for s in stacks}
        for name, entry in known.items():
            if name not in present:
                stack = build_stack(name, entry["labels"], [], self.cfg.appdata, self.cfg.keep)
                stack.last_seen = entry.get("last_seen")
                stacks.append(stack)
        return sorted(stacks, key=lambda s: s.name)

    def find(self, name: str) -> Stack | None:
        for s in self.stacks():
            if s.name == name:
                return s
        return None

    def _require(self, name: str) -> Stack:
        stack = self.find(name)
        if stack is None:
            raise OpError(f"no stack {name!r} with a backtrack.mode label on {self.cfg.machine}; to back up a folder "
                          "this agent has never seen, pass its path")
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
                                  excludes=restic_excludes(path / IGNORE_FILE, path))
        added = snap.summary.get("data_added")
        job.log(f"snapshot {snap.short_id} saved" + (f", {added} bytes added" if added is not None else ""))
        return snap

    def _backup_stack(self, stack: Stack, trigger: str, job: Job) -> Snapshot:
        if stack.error:
            raise OpError(stack.error)
        assert stack.path is not None
        if not has_files(stack.path):
            raise OpError(f"{stack.path} is missing or empty; refusing to save an empty snapshot")
        if stack.state == "absent":
            elsewhere = self.share.running_elsewhere(stack.name)
            if elsewhere:
                raise OpError(f"{stack.name} has no containers here but is running on {', '.join(elsewhere)}; a "
                              "snapshot of this copy would become its latest. Back it up there, or untrack it here.")
            job.log(f"{stack.name} has no containers here; backing up {stack.path} as it is")
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

    def backup(self, name: str, job: Job, trigger: str = "manual", path: Path | None = None) -> dict:
        """Back up one stack. `path` backs up a folder for a stack this agent has never seen (no containers, not
        remembered); a stack it knows always uses its own folder."""
        if trigger not in ("manual", "pre-migrate", "nightly"):
            raise OpError(f"unknown trigger {trigger!r}")
        with exclusive(self.cfg.state_dir, job):
            stack = self.find(name) if path is not None else None
            if stack is None and path is not None:
                if not NAME_RE.match(name):
                    raise OpError(f"invalid stack name {name!r}")
                if not path.is_absolute():
                    raise OpError("the path must be absolute")
                stack = Stack(name=name, mode="live", path=path)
            else:
                stack = self._require(name)
                if path is not None and stack.path != path:
                    raise OpError(f"{name} keeps its data in {stack.path}; leave out the path")
            try:
                snap = self._backup_stack(stack, trigger, job)
            except (OpError, ResticError, DockerError) as e:
                detail = getattr(e, "tail", "") or getattr(e, "detail", "")
                self.notifier.failure(name, "backup", str(e), detail)
                raise
        return {"snapshot": snap.short_id, "data_added": snap.summary.get("data_added")}

    def nightly(self, job: Job) -> dict:
        return self._back_up_every_stack(job, "nightly")

    def backup_all(self, job: Job) -> dict:
        """Every stack now, as the nightly run would, but tagged manual and without the heartbeat.

        The heartbeat says the schedule ran; a run started by hand must not hide a missed scheduled one.
        """
        return self._back_up_every_stack(job, "manual")

    def backup_stacks(self, job: Job, names: list[str]) -> dict:
        """The stacks in `names` one at a time, as Back up all does for every stack (a bulk backup from the hub)."""
        return self._back_up_every_stack(job, "manual", names)

    def _back_up_every_stack(self, job: Job, trigger: str, only: list[str] | None = None) -> dict:
        results: dict[str, dict] = {}
        with exclusive(self.cfg.state_dir, job):
            stacks = self.stacks()
            if only is None:
                # Nothing runs, so nothing changed since it was last backed up; and after a move this folder is
                # the old copy, which must not become the latest snapshot. Named backups still take it.
                absent = [s.name for s in stacks if s.state == "absent"]
                if absent:
                    job.log(f"skipping {', '.join(absent)}: no containers here")
                stacks = [s for s in stacks if s.state != "absent"]
            else:
                stacks = [s for s in stacks if s.name in only]
                for name in sorted(set(only) - {s.name for s in stacks}):
                    job.log(f"ERROR: {name}: no such stack on {self.cfg.machine}")
                    results[name] = {"state": "failed", "error": f"no stack {name} on {self.cfg.machine}"}
            job.log(f"{len(stacks)} stacks: {', '.join(s.name for s in stacks) or 'none'}")
            for stack in stacks:
                try:
                    snap = self._backup_stack(stack, trigger, job)
                    results[stack.name] = {"state": "ok", "snapshot": snap.short_id,
                                           "data_added": snap.summary.get("data_added")}
                except Exception as e:  # noqa: BLE001 - one stack failing must not stop the others
                    detail = getattr(e, "tail", "") or getattr(e, "detail", "")
                    job.log(f"ERROR: {stack.name}: {e}")
                    results[stack.name] = {"state": "failed", "error": str(e)}
                    self.notifier.failure(stack.name, "backup", str(e), detail)
        failed = [n for n, r in results.items() if r["state"] != "ok"]
        run = "nightly" if trigger == "nightly" else "backup-all" if only is None else "bulk-backup"
        self.notifier.summary(failed, len(results), run=run)
        if trigger == "nightly":
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
        for rel in literal_matches(old, literal_ignores(old / IGNORE_FILE, old)):
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

    def _set_aside(self, path: Path, job: Job) -> Path | None:
        if not (path.exists() or path.is_symlink()):
            return None
        aside = path.with_name(path.name + ".pre-restore")
        if aside.exists() or aside.is_symlink():
            older = aside.with_name(aside.name + "." + datetime.now().strftime("%Y%m%dT%H%M%S"))
            os.rename(aside, older)
            self.asides.moved(str(aside), str(older))
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
                if aside:
                    self.asides.add(Aside(stack=name, path=str(aside), target=str(target), created=self.clock(),
                                          job=job.id, snapshot=snap.short_id,
                                          safety_snapshot=safety.short_id if safety else None))
                target.parent.mkdir(parents=True, exist_ok=True)
                self.restic.restore(snap, target)
                job.log("files restored")
                if aside:
                    self._carry_over(aside, target, job)
                self._post_restore(name, stack.post_restore, target, snap, job)
                if aside:
                    self.asides.update(str(aside), restore_ok=True, restored_at=self.clock(),
                                       size=folder_size(aside))
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

    # -- set-aside folders ----------------------------------------------------------------------------------
    @property
    def settle(self) -> int:
        return parse_duration(self.cfg.get("PRERESTORE_SETTLE"))

    def _judge(self, aside: Aside, stacks: list[Stack] | None = None) -> Verdict:
        stacks = self.stacks() if stacks is None else stacks
        stack = next((s for s in stacks if s.name == aside.stack), None)
        return judge(aside, stack.containers if stack else None, self.docker, self.settle, self.clock())

    def prerestore(self, stacks: list[Stack] | None = None) -> list[dict]:
        """Every recorded set-aside folder with its verdict, for presence and the API."""
        items = self.asides.all()
        if not items:
            return []
        docker_error = None
        if stacks is None:
            try:
                stacks = self.stacks()
            except DockerError as e:
                stacks, docker_error = [], str(e)
        out = []
        for a in items:
            v = (Verdict("unhealthy", f"Can't check the stack: {docker_error}") if docker_error
                 else self._judge(a, stacks))
            out.append({**a.to_dict(), "state": v.state, "reason": v.reason, "removable": v.removable,
                        "containers": v.containers})
        return out

    def remove_prerestore(self, name: str, path: str, job: Job) -> dict:
        """Delete one recorded set-aside folder, after checking again that the stack is healthy."""
        with exclusive(self.cfg.state_dir, job):
            aside = self.asides.get(path)
            if aside is None or aside.stack != name:
                raise OpError(f"{path} is not a folder Backtrack set aside for {name}")
            folder = Path(aside.path)
            if not is_aside_of(folder, Path(aside.target)) or folder.is_symlink() or not folder.is_dir():
                raise OpError(f"refusing to remove {folder}: not a set-aside folder of {aside.target}")
            stack = self.find(name)
            if stack and stack.path and Path(aside.target) != stack.path:
                raise OpError(f"{folder} belongs to {aside.target}, but {name} now keeps its data in {stack.path}")
            verdict = self._judge(aside)
            if not verdict.removable:
                raise OpError(f"{name} isn't healthy yet: {verdict.reason}")
            if aside.safety_snapshot:
                try:
                    self.restic.snapshot(aside.safety_snapshot)
                except ResticError as e:
                    if "not found" not in str(e):
                        raise OpError(f"can't read the repository to check the safety snapshot: {e}", e.tail) from e
                    raise OpError(f"the safety snapshot {aside.safety_snapshot} is missing, so this folder is the "
                                  "only copy of the state before the restore; keeping it") from e
                job.log(f"safety snapshot {aside.safety_snapshot} is in the repository")
            job.log(f"{name} is healthy: {verdict.reason}")
            job.log(f"removing {folder}")
            shutil.rmtree(folder)
            self.asides.remove(aside.path)
            job.log("removed")
        return {"removed": str(folder), "size": aside.size}

    def auto_remove_prerestore(self, job: Job) -> dict:
        """PRERESTORE_AUTO_REMOVE: remove folders older than that whose stack is healthy."""
        after = parse_duration(self.cfg.get("PRERESTORE_AUTO_REMOVE"))
        removed, kept = [], []
        if not after:
            return {"removed": removed}
        for item in self.prerestore():
            if self.clock() - (item["restored_at"] or item["created"]) < after:
                continue
            if not item["removable"]:
                kept.append(item["path"])
                continue
            try:
                self.remove_prerestore(item["stack"], item["path"], job)
                removed.append(item["path"])
            except OpError as e:
                job.log(f"kept {item['path']}: {e}")
                kept.append(item["path"])
        return {"removed": removed, "kept": kept}

    def remove_prerestore_stacks(self, job: Job, names: list[str]) -> dict:
        """Remove the set-aside folders of these stacks that are safe to remove; keep the rest and say why."""
        removed, kept, failed = [], [], []
        items = [i for i in self.prerestore() if i["stack"] in names]
        job.log(f"{len(items)} set-aside folders of {', '.join(sorted(set(names)))}")
        for item in items:
            if not item["removable"]:
                job.log(f"kept {item['path']}: {item['reason']}")
                kept.append({"path": item["path"], "reason": item["reason"]})
                continue
            try:
                self.remove_prerestore(item["stack"], item["path"], job)
                removed.append(item["path"])
            except (OpError, OSError) as e:
                job.log(f"ERROR: {item['path']}: {e}")
                failed.append(item["path"])
        return {"removed": removed, "kept": kept, "failed": failed}

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

    def untrack(self, name: str) -> dict:
        """Forget a remembered stack whose containers are gone from this machine. Its snapshots stay."""
        stack = self.find(name)
        if stack is None:
            raise OpError(f"{name} isn't tracked on {self.cfg.machine}")
        if stack.containers:
            raise OpError(f"{name} still has containers on {self.cfg.machine}; remove them (docker compose down) "
                          "or its backtrack.mode label first")
        self.known.forget(name)
        return {"untracked": name}

    # -- presence -------------------------------------------------------------------------------------------
    def presence(self, current: Job | None = None, last: dict | None = None,
                 last_nightly: Job | None = None) -> dict:
        try:
            stacks = self.stacks()
            stack_list = [s.to_dict() for s in stacks]
            docker_error = None
        except DockerError as e:
            stacks, stack_list, docker_error = [], [], str(e)
        last = last or {}
        for s in stack_list:
            s["last"] = last.get(s["name"])
        lease = self.share.lease()
        return {
            "version": __version__, "url": self.cfg.get("AGENT_URL"), "hub": self.cfg.hub,
            "stacks": stack_list, "docker_error": docker_error, "prerestore": self.prerestore(None if docker_error else stacks),
            "job": current.to_dict() if current else None,
            "last_nightly": last_nightly.to_dict() if last_nightly else None,
            "maintenance": {"wants": self.cfg.maintenance, "locked": self.cfg.locked("MAINTENANCE"),
                            "holds": bool(lease and lease.machine == self.cfg.machine)},
            "time": time.time(),
        }
