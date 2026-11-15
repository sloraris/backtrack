from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backtrack.config import Config
from backtrack.docker import Container, DockerError
from backtrack.jobs import Job
from backtrack.notify import Notifier
from backtrack.ops import Agent
from backtrack.restic import Restic
from backtrack.share import Share

if not shutil.which("restic"):
    pytest.skip("restic is not installed", allow_module_level=True)


class FakeDocker:
    """Records stop/start calls; containers change state like the real thing."""

    def __init__(self):
        self.items: list[Container] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_start: set[str] = set()
        self.host_commands: list[tuple[str, dict]] = []
        self.host_result = (0, "ok\n")
        self.on_host_command = None

    def add(self, project: str, service: str, labels: dict | None = None, state: str = "running",
            depends_on: list[str] | None = None) -> Container:
        c = Container(id=f"{project}-{service}", name=f"{project}-{service}-1", project=project, service=service,
                      state=state, labels={"com.docker.compose.project": project, **(labels or {})},
                      depends_on=depends_on or [])
        self.items.append(c)
        return c

    def containers(self):
        return [Container(**c.__dict__) for c in self.items]

    def _get(self, cid):
        return next(c for c in self.items if c.id == cid)

    def stop(self, cid, timeout):
        self.calls.append(("stop", cid))
        self._get(cid).state = "exited"

    def start(self, cid):
        self.calls.append(("start", cid))
        if cid in self.fail_start:
            raise DockerError("boom")
        self._get(cid).state = "running"

    def run_host_command(self, command, env):
        self.host_commands.append((command, env))
        if self.on_host_command:
            self.on_host_command(command, env)
        return self.host_result


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__("test")
        self.failures: list[tuple] = []
        self.heartbeats: list[tuple] = []
        self.summaries: list[tuple] = []

    def failure(self, stack, step, error, detail=""):
        self.failures.append((stack, step, error))

    def heartbeat(self, ok, message):
        self.heartbeats.append((ok, message))

    def summary(self, failed, total):
        self.summaries.append((failed, total))


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory) -> Path:
    """One initialised repo, copied per test (restic init is the slow part)."""
    path = tmp_path_factory.mktemp("template") / "repo"
    env = {"RESTIC_PASSWORD": "test-password", "RESTIC_REPOSITORY": str(path), "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"}
    subprocess.run(["restic", "init"], env=env, check=True, capture_output=True)
    return path


class Lab:
    """An agent wired to a temp repo, temp appdata and a fake Docker."""

    def __init__(self, tmp: Path, repo: Path, machine: str = "alpha", share_root: Path | None = None,
                 extra_env: dict | None = None):
        self.tmp = tmp
        self.appdata = tmp / "appdata"
        self.appdata.mkdir(exist_ok=True)
        env = {
            "MACHINE": machine, "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD": "test-password",
            "APPDATA": str(self.appdata), "STATE_DIR": str(tmp / f"state-{machine}"),
            "SCRATCH": str(tmp / f"scratch-{machine}"), "SHARE_DIR": str(share_root or tmp / "share"),
            **(extra_env or {}),
        }
        self.cfg = Config.load(env)
        self.docker = FakeDocker()
        self.restic = Restic(str(repo), "test-password", cache_dir=str(tmp / "cache"))
        self.clock = [1_000_000.0]
        self.share = Share(self.cfg.share_dir, machine, clock=lambda: self.clock[0])
        self.notifier = RecordingNotifier()
        self.agent = Agent(self.cfg, self.docker, self.restic, self.share, self.notifier)

    def job(self, kind="test", stack=None) -> Job:
        return Job(kind, stack)

    def stack_dir(self, name: str, files: dict[str, str] | None = None) -> Path:
        d = self.appdata / name
        for rel, content in (files or {"data/file.txt": "hello"}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return d


@pytest.fixture
def repo(tmp_path, repo_template) -> Path:
    dst = tmp_path / "repo"
    shutil.copytree(repo_template, dst)
    return dst


@pytest.fixture
def lab(tmp_path, repo) -> Lab:
    return Lab(tmp_path, repo)
