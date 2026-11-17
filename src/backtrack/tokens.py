"""Named API tokens for the hub's public API, minted in the UI or with `backtrack token create`.

Each integration (a dashboard, a monitor, a script, an assistant) gets its own token with a scope, `read` or
`write`, so one can be revoked without touching the others. A token is shown once when it is created; the hub
keeps only its SHA-256 hash, in STATE_DIR/tokens.json, next to when it was created and last used. Tokens are
long random strings, so a plain hash is enough: there is nothing to guess from it.

The static `READ_TOKEN` and `WRITE_TOKEN` from the environment keep working alongside these.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_FILE = "tokens.json"
PREFIX = "bt_"
SCOPES = ("read", "write")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
TOUCH_EVERY = 60  # write last_used at most once a minute per token


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclass
class Token:
    id: str
    name: str
    scope: str
    hash: str
    created: float
    last_used: float | None = None

    def public(self) -> dict:
        """Everything but the hash."""
        return {k: v for k, v in asdict(self).items() if k != "hash"}


class TokenStore:
    """Thread-safe; written atomically; re-read when the file changes (the CLI may edit it)."""

    def __init__(self, state_dir: Path, clock=time.time):
        self.path = state_dir / STATE_FILE
        self._lock = threading.Lock()
        self._clock = clock
        self._cache: tuple[float | None, list[Token]] = (None, [])

    def _read(self) -> list[Token]:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return []
        if self._cache[0] == mtime:
            return list(self._cache[1])
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return []
        out = []
        for d in data.get("tokens", []):
            try:
                out.append(Token(**{k: v for k, v in d.items() if k in Token.__dataclass_fields__}))
            except TypeError:
                continue
        self._cache = (mtime, out)
        return list(out)

    def _write(self, items: list[Token]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"tokens": [asdict(t) for t in items]}, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        self._cache = (None, [])

    def list(self) -> list[Token]:
        with self._lock:
            return sorted(self._read(), key=lambda t: t.created)

    def create(self, name: str, scope: str) -> tuple[Token, str]:
        """A new token and its secret. The secret is not stored and can't be shown again."""
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("name: 1 to 64 letters, digits, spaces, '.', '_' or '-'")
        if scope not in SCOPES:
            raise ValueError("scope must be read or write")
        secret = PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            items = self._read()
            if any(t.name == name for t in items):
                raise ValueError(f"a token named {name!r} already exists")
            token = Token(id=secrets.token_hex(4), name=name, scope=scope, hash=_hash(secret),
                          created=self._clock())
            items.append(token)
            self._write(items)
        return token, secret

    def revoke(self, key: str) -> Token | None:
        """Remove a token by id or name. Returns it, or None when there was no such token."""
        with self._lock:
            items = self._read()
            gone = next((t for t in items if t.id == key), None) or next((t for t in items if t.name == key), None)
            if gone:
                self._write([t for t in items if t is not gone])
            return gone

    def check(self, given: str) -> str | None:
        """The scope of a valid token, or None. Compares hashes in constant time and records the use."""
        if not given.startswith(PREFIX):
            return None
        digest = _hash(given)
        with self._lock:
            items = self._read()
            match = None
            for t in items:  # look at every token so timing says nothing about which one matched
                if hmac.compare_digest(digest, t.hash):
                    match = t
            if not match:
                return None
            now = self._clock()
            if not match.last_used or now - match.last_used >= TOUCH_EVERY:
                match.last_used = now
                try:
                    self._write(items)
                except OSError:
                    pass  # a read-only STATE_DIR shouldn't lock integrations out
            return match.scope
