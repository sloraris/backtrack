"""Outbound HTTP: failure alerts, heartbeat pushes and stop hooks. URLs are secrets and never logged."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit, urlunsplit

log = logging.getLogger("backtrack")
USER_AGENT = "backtrack"


def safe_url(url: str) -> str:
    """Only the scheme and host, for logs."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or '?'}"


def post_json(url: str, payload: dict, timeout: float = 10, retries: int = 1) -> bool:
    data = json.dumps(payload).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if attempt < retries:
            time.sleep(1)
    log.warning("POST to %s failed", safe_url(url))
    return False


def get(url: str, timeout: float = 10) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        log.warning("GET %s failed", safe_url(url))
        return False


class Notifier:
    def __init__(self, machine: str, discord_url: str = "", webhook_url: str = "", heartbeat_url: str = ""):
        self.machine = machine
        self.discord_url = discord_url
        self.webhook_url = webhook_url
        self.heartbeat_url = heartbeat_url

    def failure(self, stack: str | None, step: str, error: str, detail: str = "") -> None:
        title = f"Backtrack: {step} failed" + (f" for {stack}" if stack else "") + f" on {self.machine}"
        if self.discord_url:
            fields = [{"name": "Machine", "value": self.machine, "inline": True},
                      {"name": "Step", "value": step, "inline": True}]
            if stack:
                fields.insert(0, {"name": "Stack", "value": stack, "inline": True})
            desc = error[:1500]
            if detail:
                desc += "\n```\n" + detail[-1800:] + "\n```"
            post_json(self.discord_url, {"username": "Backtrack", "embeds": [
                {"title": title[:250], "description": desc, "color": 0xE5484D, "fields": fields}]})
        if self.webhook_url:
            post_json(self.webhook_url, {"event": "failure", "machine": self.machine, "stack": stack,
                                         "step": step, "error": error, "detail": detail[-4000:]})

    def summary(self, failed: list[str], total: int, run: str = "nightly") -> None:
        """One message for a run over every stack (`run` is nightly or backup-all), only when something failed."""
        if not failed:
            return
        label = "Nightly run" if run == "nightly" else "Back up all"
        msg = f"{label} on {self.machine}: {len(failed)} of {total} stacks failed: {', '.join(failed)}"
        if self.discord_url:
            post_json(self.discord_url, {"username": "Backtrack", "content": msg[:1900]})
        if self.webhook_url:
            post_json(self.webhook_url, {"event": "summary", "run": run, "machine": self.machine,
                                         "failed": failed, "total": total})

    def heartbeat(self, ok: bool, message: str) -> None:
        if not self.heartbeat_url:
            return
        parts = urlsplit(self.heartbeat_url)
        query = (parts.query + "&" if parts.query else "") + urlencode(
            {"status": "up" if ok else "down", "msg": message[:200], "ping": ""})
        get(urlunsplit(parts._replace(query=query)))


def call_hooks(urls: list[str | None], payload: dict, logf) -> bool:
    """POST each hook; a failure is a warning, never fatal. Returns True when all succeeded."""
    ok = True
    for url in urls:
        if not url:
            continue
        if post_json(url, payload):
            logf(f"hook {payload['event']} -> {safe_url(url)}: ok")
        else:
            ok = False
            logf(f"WARNING: hook {payload['event']} -> {safe_url(url)} failed; continuing")
    return ok
