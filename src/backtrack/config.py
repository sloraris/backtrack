"""Configuration: environment first, then settings saved on the agent, then defaults.

Anything set in the environment is locked (the hub UI shows it read-only). Settable keys that are not in the
environment can be changed through the agent API and are saved in STATE_DIR/settings.json.
Secrets also accept NAME_FILE pointing at a file.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

SECRETS = {
    "RESTIC_PASSWORD", "CLUSTER_KEY", "UI_PASSWORD", "READ_TOKEN", "WRITE_TOKEN",
    "DISCORD_WEBHOOK_URL", "ALERT_WEBHOOK_URL", "HEARTBEAT_URL",
    "HOOK_PRE_STOP_URL", "HOOK_POST_START_URL",
}

DEFAULTS: dict[str, str] = {
    "MACHINE": "",
    "AGENT_URL": "",
    "RESTIC_REPOSITORY": "/share/restic",
    "SHARE_DIR": "/share/backtrack",
    "APPDATA": "/opt/appdata",
    "STATE_DIR": "/opt/appdata/backtrack",
    "SCRATCH": "/opt/cache/backtrack",
    "TZ": "UTC",
    "BACKUP_SCHEDULE": "04:00",
    "KEEP": "daily=7,weekly=4,monthly=6",
    "MAINTENANCE": "false",
    "PRUNE_SCHEDULE": "sun 13:00",
    "VERIFY_SCHEDULE": "1 14:00",
    "HUB": "false",
    "THEME": "default",
    "LISTEN": "0.0.0.0:8080",
    "RESTIC_BINARY": "restic",
    "DOCKER_SOCKET": "/var/run/docker.sock",
    "HELPER_IMAGE": "",
    "STOP_TIMEOUT": "60",
    "PRESENCE_STALE_SECONDS": "180",
    "LEASE_STALE_DAYS": "8",
    **{k: "" for k in SECRETS},
}

# Keys the hub UI may change when the environment doesn't set them.
SETTABLE = ("BACKUP_SCHEDULE", "KEEP", "PRUNE_SCHEDULE", "VERIFY_SCHEDULE", "THEME", "MAINTENANCE")

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = r"([01]\d|2[0-3]):[0-5]\d"
VALIDATORS = {
    "BACKUP_SCHEDULE": re.compile(rf"^{_HHMM}$"),
    "PRUNE_SCHEDULE": re.compile(rf"^({'|'.join(_DAYS)}) {_HHMM}$"),
    "VERIFY_SCHEDULE": re.compile(rf"^([1-9]|1\d|2[0-8]) {_HHMM}$"),
    "KEEP": re.compile(r"^(daily|weekly|monthly|yearly|last|hourly)=\d+(,(daily|weekly|monthly|yearly|last|hourly)=\d+)*$"),
    "THEME": re.compile(r"^(default|orbit)$"),
    "MAINTENANCE": re.compile(r"^(true|false)$"),
}


class ConfigError(Exception):
    pass


def parse_keep(spec: str) -> dict[str, int]:
    """'daily=7,weekly=4' -> {'daily': 7, 'weekly': 4}."""
    if not VALIDATORS["KEEP"].match(spec.strip()):
        raise ConfigError(f"invalid retention {spec!r}")
    out = {}
    for part in spec.strip().split(","):
        k, v = part.split("=")
        out[k] = int(v)
    return out


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    env: dict[str, str]
    saved: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        resolved: dict[str, str] = {}
        for key in DEFAULTS:
            if key in env and env[key] != "":
                resolved[key] = env[key]
            elif f"{key}_FILE" in env and env[f"{key}_FILE"]:
                resolved[key] = Path(env[f"{key}_FILE"]).read_text().strip()
        cfg = cls(env=resolved)
        cfg.saved = cfg._read_saved()
        return cfg

    # -- lookup ---------------------------------------------------------------------------------------------
    def get(self, key: str) -> str:
        if key in self.env:
            return self.env[key]
        with self._lock:
            if key in SETTABLE and key in self.saved:
                return self.saved[key]
        return DEFAULTS[key]

    def locked(self, key: str) -> bool:
        return key in self.env

    def __getattr__(self, name: str):
        key = name.upper()
        if key in DEFAULTS:
            return self.get(key)
        raise AttributeError(name)

    @property
    def machine(self) -> str:
        return self.get("MACHINE")

    @property
    def appdata(self) -> Path:
        return Path(self.get("APPDATA"))

    @property
    def state_dir(self) -> Path:
        return Path(self.get("STATE_DIR"))

    @property
    def share_dir(self) -> Path:
        return Path(self.get("SHARE_DIR"))

    @property
    def scratch(self) -> Path:
        return Path(self.get("SCRATCH"))

    @property
    def hub(self) -> bool:
        return _truthy(self.get("HUB"))

    @property
    def maintenance(self) -> bool:
        return _truthy(self.get("MAINTENANCE"))

    @property
    def keep(self) -> dict[str, int]:
        return parse_keep(self.get("KEEP"))

    def validate(self) -> None:
        if not self.machine:
            raise ConfigError("MACHINE is required")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", self.machine):
            raise ConfigError("MACHINE must be lowercase letters, digits, '.', '_' or '-'")
        if not self.get("RESTIC_PASSWORD"):
            raise ConfigError("RESTIC_PASSWORD is required")
        for key in SETTABLE:
            if not VALIDATORS[key].match(self.get(key)):
                raise ConfigError(f"invalid {key}: {self.get(key)!r}")
        if self.hub:
            for key in ("UI_PASSWORD", "CLUSTER_KEY"):
                if not self.get(key):
                    raise ConfigError(f"{key} is required when HUB=true")

    # -- saved settings -------------------------------------------------------------------------------------
    @property
    def _settings_path(self) -> Path:
        return Path(self.env.get("STATE_DIR", DEFAULTS["STATE_DIR"])) / "settings.json"

    def _read_saved(self) -> dict[str, str]:
        try:
            data = json.loads(self._settings_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {k: str(v) for k, v in data.items() if k in SETTABLE}

    def settings(self) -> list[dict]:
        return [
            {"key": k, "value": self.get(k), "locked": self.locked(k), "source":
             "env" if self.locked(k) else ("saved" if k in self.saved else "default")}
            for k in SETTABLE
        ]

    def update_settings(self, changes: dict[str, str]) -> list[str]:
        """Apply UI changes. Returns errors; nothing is saved if there are any."""
        errors = []
        clean = {}
        for k, v in changes.items():
            v = str(v).strip()
            if k not in SETTABLE:
                errors.append(f"{k} is not a setting")
            elif self.locked(k):
                errors.append(f"{k} is set in the environment")
            elif not VALIDATORS[k].match(v):
                errors.append(f"invalid {k}: {v!r}")
            else:
                clean[k] = v
        if errors:
            return errors
        with self._lock:
            self.saved.update(clean)
            path = self._settings_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.saved, indent=2, sort_keys=True))
            os.replace(tmp, path)
        return []
