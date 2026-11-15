"""Stack discovery from compose labels, and helpers for stack folders."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from backtrack.config import ConfigError, parse_keep
from backtrack.docker import Container

PREFIX = "backtrack."
L_MODE = PREFIX + "mode"
L_PATH = PREFIX + "path"
L_KEEP = PREFIX + "keep"
L_PRE_STOP = PREFIX + "hook.pre-stop"
L_POST_START = PREFIX + "hook.post-start"
L_POST_RESTORE = PREFIX + "post-restore"
STACK_LABELS = (L_MODE, L_PATH, L_KEEP, L_PRE_STOP, L_POST_START, L_POST_RESTORE)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
IGNORE_FILE = ".backupignore"


@dataclass
class Stack:
    name: str
    mode: str | None = None
    path: Path | None = None
    keep: dict[str, int] | None = None
    pre_stop: str | None = None
    post_start: str | None = None
    post_restore: str | None = None
    containers: list[Container] = field(default_factory=list)
    error: str | None = None

    @property
    def state(self) -> str:
        if not self.containers:
            return "absent"
        running = sum(c.running for c in self.containers)
        if running == len(self.containers):
            return "running"
        return "stopped" if running == 0 else "partial"

    def start_order(self) -> list[Container]:
        """Containers ordered so dependencies start first (compose depends_on)."""
        by_service = {c.service: c for c in self.containers}
        order: list[Container] = []
        seen: set[str] = set()

        def visit(svc: str, trail: tuple[str, ...]) -> None:
            if svc in seen or svc not in by_service or svc in trail:
                return
            for dep in by_service[svc].depends_on:
                visit(dep, trail + (svc,))
            seen.add(svc)
            order.append(by_service[svc])

        for c in sorted(self.containers, key=lambda c: c.service):
            visit(c.service, ())
        order += [c for c in self.containers if c not in order]  # containers without a service label
        return order

    def stop_order(self) -> list[Container]:
        return list(reversed(self.start_order()))

    def to_dict(self) -> dict:
        return {
            "name": self.name, "mode": self.mode, "path": str(self.path) if self.path else None,
            "keep": self.keep, "state": self.state, "error": self.error,
            "containers": [{"name": c.name, "service": c.service, "state": c.state} for c in self.containers],
            "hooks": {"pre_stop": bool(self.pre_stop), "post_start": bool(self.post_start)},
            "post_restore": bool(self.post_restore),
        }


def discover(containers: list[Container], appdata: Path, default_keep: dict[str, int]) -> list[Stack]:
    """Group containers by compose project; keep projects with a backtrack.mode label."""
    projects: dict[str, list[Container]] = {}
    for c in containers:
        projects.setdefault(c.project, []).append(c)

    stacks = []
    for name, members in sorted(projects.items()):
        if not any(L_MODE in c.labels for c in members):
            continue
        stack = Stack(name=name, containers=members)
        values: dict[str, str] = {}
        for label in STACK_LABELS:
            found = {c.labels[label] for c in members if label in c.labels}
            if len(found) > 1:
                stack.error = f"containers disagree on {label}"
            elif found:
                values[label] = found.pop()
        try:
            if not NAME_RE.match(name):
                raise ConfigError(f"unsupported project name {name!r}")
            mode = values.get(L_MODE, "")
            if mode not in ("stop", "live"):
                raise ConfigError(f"{L_MODE} must be 'stop' or 'live', not {mode!r}")
            stack.mode = mode
            path = Path(values[L_PATH]) if L_PATH in values else appdata / name
            if not path.is_absolute():
                raise ConfigError(f"{L_PATH} must be absolute")
            stack.path = path
            stack.keep = parse_keep(values[L_KEEP]) if L_KEEP in values else dict(default_keep)
        except ConfigError as e:
            stack.error = stack.error or str(e)
        stack.pre_stop = values.get(L_PRE_STOP) or None
        stack.post_start = values.get(L_POST_START) or None
        stack.post_restore = values.get(L_POST_RESTORE) or None
        stacks.append(stack)
    return stacks


def has_files(path: Path) -> bool:
    """True when the folder holds at least one non-directory entry other than .backupignore.

    A tree of empty folders counts as empty: deploy steps often `mkdir -p` a stack's data folder before
    starting it, and that must not hide a missing restore.
    """
    if not path.is_dir():
        return False
    for root, dirs, files in os.walk(path, followlinks=False):
        names = [f for f in files if not (root == str(path) and f == IGNORE_FILE)]
        if names:
            return True
        # symlinks to directories show up in dirs but are real content
        if any(os.path.islink(os.path.join(root, d)) for d in dirs):
            return True
    return False


def literal_ignores(ignore_file: Path, stack_path: Path) -> list[Path]:
    """Literal (wildcard-free) entries of a .backupignore, as paths relative to the stack folder."""
    try:
        lines = ignore_file.read_text().splitlines()
    except (FileNotFoundError, UnicodeDecodeError):
        return []
    out = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if any(ch in line for ch in "*?[]\\$"):
            continue
        p = Path(line.rstrip("/"))
        if p.is_absolute():
            try:
                p = p.relative_to(stack_path)
            except ValueError:
                continue
        if not p.parts or ".." in p.parts:
            continue
        out.append(p)
    return out
