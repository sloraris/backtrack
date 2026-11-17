"""A tiny Docker Engine API client over the unix socket (stdlib only)."""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import struct
from datetime import datetime
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote, urlencode

# The API version is taken from the daemon (GET /_ping), never pinned: Docker 29 refuses clients older than 1.44,
# and everything Backtrack calls (list, stop, start, create, wait, logs, delete) is the same across versions.
_VERSION_RE = re.compile(r"\d+\.\d+")


class DockerError(Exception):
    pass


@dataclass
class Container:
    id: str
    name: str
    project: str
    service: str
    state: str  # running, exited, created, paused, restarting, dead
    labels: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.state in ("running", "restarting", "paused")


@dataclass
class Health:
    """What `docker inspect` says about a container's health right now."""
    running: bool
    health: str | None  # healthy, unhealthy, starting; None without a healthcheck
    started_at: float | None  # epoch seconds of the last (re)start
    restarts: int = 0


class DockerClient(Protocol):
    def containers(self) -> list[Container]: ...
    def inspect(self, container_id: str) -> Health: ...
    def stop(self, container_id: str, timeout: int) -> None: ...
    def start(self, container_id: str) -> None: ...
    def run_host_command(self, command: str, env: dict[str, str]) -> tuple[int, str]: ...


def parse_docker_time(value: str | None) -> float | None:
    """'2026-01-02T03:04:05.123456789Z' -> epoch seconds. Docker's zero time and junk give None."""
    if not value or value.startswith("0001-"):
        return None
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$", value)
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]
    tz = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        return datetime.fromisoformat(m.group(1) + frac + tz).timestamp()
    except ValueError:
        return None


def parse_depends_on(label: str) -> list[str]:
    """com.docker.compose.depends_on looks like 'db:service_healthy:false,cache:service_started:false'."""
    out = []
    for part in (label or "").split(","):
        name = part.strip().split(":")[0]
        if name:
            out.append(name)
    return out


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class Docker:
    def __init__(self, socket_path: str = "/var/run/docker.sock", helper_image: str = ""):
        self.socket_path = socket_path
        self.helper_image = helper_image
        self._api: str | None = None

    def _raw(self, method: str, path: str, body=None, timeout: float = 120) -> tuple[int, bytes, dict[str, str]]:
        conn = _UnixConnection(self.socket_path, timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.getheaders()}
        except OSError as e:
            raise DockerError(f"docker socket: {e}") from e
        finally:
            conn.close()

    def api_prefix(self) -> str:
        """`/v<daemon's API version>`, or no prefix (the daemon's own default) if it doesn't say."""
        if self._api is None:
            _, _, headers = self._raw("GET", "/_ping", timeout=30)
            version = headers.get("api-version", "")
            self._api = f"/v{version}" if _VERSION_RE.fullmatch(version) else ""
        return self._api

    def _request(self, method: str, path: str, body=None, timeout: float = 120) -> tuple[int, bytes]:
        status, raw, _ = self._raw(method, self.api_prefix() + path, body, timeout)
        return status, raw

    def _json(self, method: str, path: str, body=None, ok=(200, 201, 204, 304), timeout: float = 120):
        status, raw = self._request(method, path, body, timeout)
        if status not in ok:
            msg = raw.decode(errors="replace")
            try:
                msg = json.loads(raw).get("message", msg)
            except (ValueError, AttributeError):
                pass
            raise DockerError(f"{method} {path}: {status} {msg}")
        return json.loads(raw) if raw.strip() else None

    def containers(self) -> list[Container]:
        items = self._json("GET", "/containers/json?" + urlencode({"all": "1"}))
        out = []
        for c in items:
            labels = c.get("Labels") or {}
            project = labels.get("com.docker.compose.project")
            if not project:
                continue
            out.append(Container(
                id=c["Id"],
                name=(c.get("Names") or ["/" + c["Id"][:12]])[0].lstrip("/"),
                project=project,
                service=labels.get("com.docker.compose.service", ""),
                state=c.get("State", ""),
                labels=labels,
                depends_on=parse_depends_on(labels.get("com.docker.compose.depends_on", "")),
            ))
        return out

    def inspect(self, container_id: str) -> Health:
        info = self._json("GET", f"/containers/{quote(container_id)}/json")
        state = info.get("State") or {}
        return Health(running=bool(state.get("Running")),
                      health=(state.get("Health") or {}).get("Status") or None,
                      started_at=parse_docker_time(state.get("StartedAt")),
                      restarts=int(info.get("RestartCount") or 0))

    def stop(self, container_id: str, timeout: int) -> None:
        self._json("POST", f"/containers/{quote(container_id)}/stop?t={int(timeout)}", timeout=timeout + 60)

    def start(self, container_id: str) -> None:
        self._json("POST", f"/containers/{quote(container_id)}/start")

    def own_image(self) -> str:
        if self.helper_image:
            return self.helper_image
        cid = os.environ.get("HOSTNAME", "")
        info = self._json("GET", f"/containers/{quote(cid)}/json")
        return info["Config"]["Image"]

    def run_host_command(self, command: str, env: dict[str, str]) -> tuple[int, str]:
        """Run `command` in the host's namespaces through a short-lived privileged helper container."""
        spec = {
            "Image": self.own_image(),
            "Entrypoint": ["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "sh", "-c", command],
            "Cmd": [],
            "Env": [f"{k}={v}" for k, v in env.items()],
            "Labels": {"backtrack.helper": "true"},
            "HostConfig": {"Privileged": True, "PidMode": "host", "NetworkMode": "host"},
        }
        created = self._json("POST", "/containers/create", spec)
        cid = created["Id"]
        try:
            self._json("POST", f"/containers/{cid}/start")
            result = self._json("POST", f"/containers/{cid}/wait", timeout=3600)
            status, raw = self._request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1")
            return int(result.get("StatusCode", 1)), demux_logs(raw)
        finally:
            try:
                self._json("DELETE", f"/containers/{cid}?force=1")
            except DockerError:
                pass


def demux_logs(raw: bytes) -> str:
    """Docker multiplexes stdout/stderr in 8-byte-header frames when the container has no TTY."""
    out, i = [], 0
    while i + 8 <= len(raw):
        size = struct.unpack(">I", raw[i + 4:i + 8])[0]
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if i == 0 and raw:
        return raw.decode(errors="replace")
    return b"".join(out).decode(errors="replace")
