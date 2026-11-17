"""Jobs: one at a time per agent, queued, with a log and a short history."""

from __future__ import annotations

import fcntl
import json
import logging
import queue
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

log = logging.getLogger("backtrack")
HISTORY = 200


class Job:
    def __init__(self, kind: str, stack: str | None = None, params: dict | None = None,
                 echo: Callable[[str], None] | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.stack = stack
        self.params = params or {}
        self.state = "queued"
        self.lines: list[str] = []
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.error: str | None = None
        self.result: dict = {}
        self._echo = echo

    def log(self, msg: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        self.lines.append(line)
        if len(self.lines) > 2000:
            del self.lines[:500]
        log.info("[%s %s] %s", self.kind, self.stack or "-", msg)
        if self._echo:
            self._echo(msg)

    def to_dict(self, with_log: bool = False) -> dict:
        d = {"id": self.id, "kind": self.kind, "stack": self.stack, "params": self.params, "state": self.state,
             "created": self.created, "started": self.started, "finished": self.finished, "error": self.error,
             "result": self.result}
        if with_log:
            d["log"] = self.lines
        return d


@contextmanager
def exclusive(state_dir: Path, job: Job | None = None, timeout: float = 4 * 3600):
    """Host-wide job lock, shared by the server and `docker exec backtrack ...` CLI runs."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "job.lock", "a+") as fh:
        deadline = time.time() + timeout
        waited = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waited and job:
                    job.log("waiting for another job on this host to finish")
                    waited = True
                if time.time() > deadline:
                    raise TimeoutError("another job held the host lock for too long")
                time.sleep(2)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


class JobRunner:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self._queue: queue.Queue[tuple[Job, Callable[[Job], dict | None]]] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()
        self.current: Job | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="jobs", daemon=True)
        self._load_history()

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait: float = 600) -> None:
        """Finish the running job (a restore must not be cut in half), drop queued ones."""
        self._stop.set()
        self._queue.put(None)  # type: ignore[arg-type]
        self._thread.join(wait)

    def submit(self, kind: str, stack: str | None, fn: Callable[[Job], dict | None],
               params: dict | None = None) -> Job:
        job = Job(kind, stack, params)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > HISTORY:
                self._jobs.pop(self._order.popleft(), None)
        self._queue.put((job, fn))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, n: int = 50) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in list(self._order)[-n:][::-1] if i in self._jobs]

    def queued(self) -> list[Job]:
        return [j for j in self.recent(HISTORY) if j.state == "queued"]

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None or self._stop.is_set():
                break
            job, fn = item
            self.current = job
            job.state, job.started = "running", time.time()
            try:
                job.result = fn(job) or {}
                job.state = "ok" if not job.result.get("failed") else "failed"
            except Exception as e:  # noqa: BLE001 - every failure is reported on the job
                job.state, job.error = "failed", str(e)
                job.log(f"ERROR: {e}")
            finally:
                job.finished = time.time()
                self.current = None
                self._append_history(job)

    # -- history on disk ------------------------------------------------------------------------------------
    @property
    def _history_path(self) -> Path:
        return self.state_dir / "history.jsonl"

    def _append_history(self, job: Job) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            entry = job.to_dict()
            entry["log"] = job.lines[-200:]
            with open(self._history_path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
            lines = self._history_path.read_text().splitlines()
            if len(lines) > HISTORY * 2:
                self._history_path.write_text("\n".join(lines[-HISTORY:]) + "\n")
        except OSError as e:
            log.warning("could not write job history: %s", e)

    def _load_history(self) -> None:
        try:
            lines = self._history_path.read_text().splitlines()[-HISTORY:]
        except OSError:
            return
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            job = Job(d["kind"], d.get("stack"), d.get("params"))
            job.id, job.state, job.created = d["id"], d["state"], d["created"]
            job.started, job.finished, job.error = d.get("started"), d.get("finished"), d.get("error")
            job.result, job.lines = d.get("result") or {}, d.get("log") or []
            self._jobs[job.id] = job
            self._order.append(job.id)

    def last_results(self) -> dict[str, dict]:
        """Latest finished backup per stack: {stack: {time, state, error, snapshot}}."""
        out: dict[str, dict] = {}
        for job in self.recent(HISTORY)[::-1]:
            if job.finished is None:
                continue
            if job.kind == "backup" and job.stack:
                out[job.stack] = {"time": job.finished, "state": job.state, "error": job.error,
                                  "snapshot": job.result.get("snapshot"),
                                  "data_added": job.result.get("data_added")}
            elif job.kind in ("nightly", "backup-all"):
                for name, r in (job.result.get("stacks") or {}).items():
                    out[name] = {"time": job.finished, **r}
        return out

    def last_of(self, kind: str) -> Job | None:
        for job in self.recent(HISTORY):
            if job.kind == kind and job.finished is not None:
                return job
        return None
