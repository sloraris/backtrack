"""Command line: `backtrack serve` runs the agent; the other commands are for `docker exec backtrack backtrack ...`."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

from backtrack import __version__
from backtrack.config import Config, ConfigError
from backtrack.jobs import Job


def _job(kind: str, stack: str | None = None) -> Job:
    return Job(kind, stack, echo=lambda msg: print(msg, flush=True))


def _run(fn, job: Job) -> int:
    try:
        result = fn(job)
    except Exception as e:  # noqa: BLE001 - print a clear message instead of a traceback
        print(f"ERROR: {e}", file=sys.stderr)
        tail = getattr(e, "tail", "") or getattr(e, "detail", "")
        if tail:
            print(tail, file=sys.stderr)
        return 1
    if isinstance(result, dict) and result:
        print(json.dumps(result, indent=2, default=str))
        if result.get("failed"):
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="backtrack", description="Back up Docker Compose stacks with restic.")
    p.add_argument("--version", action="version", version=f"backtrack {__version__}")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the agent (scheduler, API, and the hub UI when HUB=true)")
    sub.add_parser("init", help="create the restic repository if it doesn't exist")
    sub.add_parser("list", help="stacks this agent can see")
    sp = sub.add_parser("snapshots", help="snapshots of one stack, or all")
    sp.add_argument("stack", nargs="?")
    b = sub.add_parser("backup", help="back up one stack now")
    b.add_argument("stack")
    b.add_argument("--trigger", choices=["manual", "pre-migrate"], default="manual")
    sub.add_parser("nightly", help="back up every stack now, as the nightly run would")
    r = sub.add_parser("restore", help="restore a stack in place")
    r.add_argument("stack")
    r.add_argument("--snapshot", default="latest")
    r.add_argument("--path", type=Path, help="data folder, when the stack has no containers here")
    r.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    a = sub.add_parser("auto-restore", help="restore latest if the stack's folder is missing or empty (pre-deploy)")
    a.add_argument("stack")
    a.add_argument("--path", type=Path)
    t = sub.add_parser("restore-test", help="restore a random (or the given) stack to scratch and compare")
    t.add_argument("stack", nargs="?")
    pr = sub.add_parser("prune", help="restic prune (maintenance agent only unless --force)")
    pr.add_argument("--force", action="store_true")
    ck = sub.add_parser("check", help="restic check with a data subset")
    ck.add_argument("--force", action="store_true")
    ck.add_argument("--subset")
    fs = sub.add_parser("forget-stack", help="delete every snapshot of a stack that no longer exists")
    fs.add_argument("stack")
    fs.add_argument("--yes", action="store_true")

    pa = sub.add_parser("prerestore", help="folders restores set aside: list them, or remove the safe ones")
    pa_sub = pa.add_subparsers(dest="action", required=True)
    pa_sub.add_parser("list", help="every set-aside folder and whether it is safe to remove")
    pr_rm = pa_sub.add_parser("remove", help="remove a stack's set-aside folders once the stack is healthy")
    pr_rm.add_argument("stack")
    pr_rm.add_argument("--path", help="just this folder (default: every one of the stack that is safe)")
    pr_rm.add_argument("--yes", action="store_true")

    args = p.parse_args(argv)
    if not args.cmd:
        args.cmd = "serve"

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr if args.cmd != "serve" else sys.stdout)
    if args.cmd != "serve":
        logging.getLogger("backtrack").setLevel(logging.WARNING)  # the job echo already prints progress

    try:
        cfg = Config.load()
        cfg.validate()
    except (ConfigError, OSError) as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2

    from backtrack.service import Service, build_agent
    agent = build_agent(cfg)

    if args.cmd == "serve":
        return _serve(cfg, agent)
    if args.cmd == "init":
        if agent.restic.is_initialized():
            print("repository already initialised")
            return 0
        agent.restic.init()
        print("repository created")
        return 0
    if args.cmd == "list":
        try:
            stacks = agent.stacks()
        except Exception as e:  # noqa: BLE001 - docker socket missing, daemon refusing, ...
            print(f"error: {e}", file=sys.stderr)
            return 1
        for s in stacks:
            print(f"{s.name:24} {s.mode or '-':5} {s.state:8} {s.path}" + (f"  ERROR: {s.error}" if s.error else ""))
        return 0
    if args.cmd == "snapshots":
        for s in agent.restic.snapshots(args.stack):
            print(f"{s.short_id}  {s.time:%Y-%m-%d %H:%M}  {s.host:20} {s.tag('machine') or '-':10} "
                  f"{s.tag('trigger') or '-'}")
        return 0
    if args.cmd == "backup":
        return _run(lambda j: agent.backup(args.stack, j, args.trigger), _job("backup", args.stack))
    if args.cmd == "nightly":
        return _run(agent.nightly, _job("nightly"))
    if args.cmd == "restore":
        if not args.yes and not _confirm(f"Restore {args.stack} from {args.snapshot}? The current folder is kept as "
                                         f"{args.stack}.pre-restore. Type the stack name to continue: ", args.stack):
            return 1
        return _run(lambda j: agent.restore(args.stack, j, args.snapshot, args.path), _job("restore", args.stack))
    if args.cmd == "auto-restore":
        return agent.auto_restore(args.stack, _job("auto-restore", args.stack), args.path)
    if args.cmd == "restore-test":
        return _run(lambda j: agent.restore_test(j, args.stack), _job("restore-test", args.stack))
    if args.cmd == "prune":
        return _run(lambda j: agent.prune(j, args.force), _job("prune"))
    if args.cmd == "check":
        return _run(lambda j: agent.check(j, args.force, args.subset), _job("check"))
    if args.cmd == "forget-stack":
        if not args.yes and not _confirm(f"Delete every snapshot of {args.stack}? This can't be undone after the next "
                                         f"prune. Type the stack name to continue: ", args.stack):
            return 1
        return _run(lambda j: agent.forget_stack(args.stack, j), _job("forget-stack", args.stack))
    if args.cmd == "prerestore":
        return _prerestore(agent, args)
    return 2


def _prerestore(agent, args) -> int:
    from backtrack import ui
    items = agent.prerestore()
    if args.action == "list":
        if not items:
            print("no set-aside folders")
        for f in items:
            print(f"{f['stack']:20} {f['state']:10} {ui.size(f.get('size')):>9}  {f['path']}  ({f['reason']})")
        return 0
    chosen = [f for f in items if f["stack"] == args.stack and (not args.path or f["path"] == args.path)]
    if not chosen:
        print(f"{args.stack} has no set-aside folder" + (f" {args.path}" if args.path else ""), file=sys.stderr)
        return 1
    if not args.path:
        waiting = [f for f in chosen if not f["removable"]]
        for f in waiting:
            print(f"keeping {f['path']}: {f['reason']}")
        chosen = [f for f in chosen if f["removable"]]
        if not chosen:
            return 1
    paths = ", ".join(f["path"] for f in chosen)
    if not args.yes and not _confirm(f"Remove {paths}? Type the stack name to continue: ", args.stack):
        return 1
    code = 0
    for f in chosen:
        code |= _run(lambda j, path=f["path"]: agent.remove_prerestore(args.stack, path, j),
                     _job("prerestore-remove", args.stack))
    return code


def _confirm(prompt: str, expected: str) -> bool:
    if not sys.stdin.isatty():
        print("refusing without a terminal; pass --yes", file=sys.stderr)
        return False
    return input(prompt).strip() == expected


def _serve(cfg: Config, agent) -> int:
    from backtrack.server import serve
    from backtrack.service import Service

    log = logging.getLogger("backtrack")
    if not agent.restic.is_initialized():
        log.warning("restic repository %s is not initialised (or not reachable); run `backtrack init` once",
                    cfg.get("RESTIC_REPOSITORY"))
    service = Service(cfg, agent)
    service.start()
    httpd = serve(service, cfg.get("LISTEN"))
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.info("signal %s: finishing the running job, then stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("backtrack %s on %s, machine %s%s; next runs: %s", __version__, cfg.get("LISTEN"), cfg.machine,
             " (hub)" if cfg.hub else "", {k: v.isoformat() for k, v in service.next_runs.items()})
    stop.wait()
    httpd.shutdown()
    service.stop()
    return 0
