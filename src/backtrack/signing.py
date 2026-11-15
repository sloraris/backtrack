"""Hub-to-agent request signing with the shared cluster key (HMAC-SHA256)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

SKEW = 60
H_TS, H_NONCE, H_SIG = "X-Backtrack-Timestamp", "X-Backtrack-Nonce", "X-Backtrack-Signature"


def _mac(key: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    msg = b"\n".join([method.upper().encode(), path.encode(), ts.encode(), nonce.encode(),
                      hashlib.sha256(body).hexdigest().encode()])
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def sign(key: str, method: str, path: str, body: bytes = b"", now: float | None = None) -> dict[str, str]:
    ts = str(int(now if now is not None else time.time()))
    nonce = secrets.token_hex(16)
    return {H_TS: ts, H_NONCE: nonce, H_SIG: _mac(key, method, path, ts, nonce, body)}


class Verifier:
    def __init__(self, key: str, clock=time.time):
        self.key = key
        self.clock = clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, method: str, path: str, headers, body: bytes) -> bool:
        if not self.key:
            return False
        ts, nonce, sig = headers.get(H_TS, ""), headers.get(H_NONCE, ""), headers.get(H_SIG, "")
        if not (ts.isdigit() and nonce and sig):
            return False
        now = self.clock()
        if abs(now - int(ts)) > SKEW:
            return False
        if not hmac.compare_digest(sig, _mac(self.key, method, path, ts, nonce, body)):
            return False
        with self._lock:
            for n, t in list(self._seen.items()):
                if now - t > 2 * SKEW:
                    del self._seen[n]
            if nonce in self._seen:
                return False  # replay
            self._seen[nonce] = now
        return True
