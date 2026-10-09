"""Single-user admin authentication: signed session cookie + login throttling."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from starlette.requests import Request

from .config import ADMIN_BASE, Config

COOKIE = "refused_session"
MAX_FAILS = 5
FAIL_WINDOW = 15 * 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Auth:
    def __init__(self, config: Config):
        self.cfg = config
        self._secret = self._derive_secret()
        self._fails: dict[str, list[float]] = {}

    def _derive_secret(self) -> bytes:
        """Random key persisted in the data dir, bound to the current password:
        sessions survive restarts but die when the password is changed."""
        path = self.cfg.data_dir / "session.key"
        try:
            key = path.read_bytes()
            if len(key) < 32:
                raise OSError("short key")
        except OSError:
            key = secrets.token_bytes(32)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(key)
            except OSError:
                pass  # in-memory key only: sessions end on restart
        bound = f"refused-v1|{self.cfg.username}|{self.cfg.password}".encode()
        return hmac.new(key, bound, hashlib.sha256).digest()

    # ---- credentials -----------------------------------------------------
    def check_credentials(self, username: str, password: str) -> bool:
        ok_user = hmac.compare_digest(username.encode(), self.cfg.username.encode())
        ok_pass = hmac.compare_digest(password.encode(), self.cfg.password.encode())
        return ok_user and ok_pass

    # ---- throttling --------------------------------------------------------
    def retry_after(self, ip: str) -> int:
        now = time.time()
        fails = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
        self._fails[ip] = fails
        if len(fails) >= MAX_FAILS:
            return max(1, int(fails[-MAX_FAILS] + FAIL_WINDOW - now))
        return 0

    def record_failure(self, ip: str) -> None:
        if len(self._fails) > 2000:
            self._fails.clear()
        self._fails.setdefault(ip, []).append(time.time())

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)

    # ---- sessions ------------------------------------------------------------
    def issue(self) -> tuple[str, str]:
        csrf = secrets.token_hex(16)
        payload = _b64(
            json.dumps(
                {"u": self.cfg.username,
                 "exp": time.time() + self.cfg.session_hours * 3600,
                 "csrf": csrf},
                separators=(",", ":"),
            ).encode()
        )
        sig = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}", csrf

    def verify(self, request: Request) -> dict | None:
        raw = request.cookies.get(COOKIE)
        if not raw or "." not in raw:
            return None
        payload, _, sig = raw.rpartition(".")
        good = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        if data.get("u") != self.cfg.username:
            return None
        return data

    def cookie_kwargs(self) -> dict:
        return {
            "httponly": True,
            "samesite": "strict",
            "secure": self.cfg.cookie_secure,
            "path": ADMIN_BASE,
            "max_age": int(self.cfg.session_hours * 3600),
        }
