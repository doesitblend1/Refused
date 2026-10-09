"""Environment-driven configuration for Refused.

Everything that is a *secret* or a *deployment fact* lives here (env vars).
Everything that is a *behaviour knob* lives in settings.py and is edited from
the admin panel at /refused.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VERSION = "1.8.0"
ADMIN_BASE = "/refused"


class ConfigError(RuntimeError):
    """Raised at startup when the environment is unusable."""


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass
class Config:
    # Where Remux is reachable from inside this container.
    remux_url: str = "http://remux:3000"

    # Admin panel credentials (required).
    username: str = ""
    password: str = ""

    # Persistent state: settings.json, refused.db, session key.
    data_dir: Path = Path("/data")

    # Seconds to wait on a normal upstream request.
    request_timeout: float = 30.0

    # Admin session lifetime.
    session_hours: float = 12.0

    # Set true only when the panel is served over HTTPS.
    cookie_secure: bool = False

    log_level: str = "INFO"

    # First-run defaults for the module toggles (the panel wins afterwards).
    stream_strip_default: bool = False
    poster_refresh_default: bool = False
    see_all_default: bool = False
    search_clean_default: bool = False
    diagnostics_default: bool = False

    # JSON bodies larger than this are streamed through untouched.
    max_json_mb: int = 32

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls(
            remux_url=_env_str("REMUX_URL", "http://remux:3000").rstrip("/"),
            username=_env_str("REFUSED_USERNAME"),
            password=os.getenv("REFUSED_PASSWORD", "").strip(),
            data_dir=Path(_env_str("DATA_DIR", "/data")),
            request_timeout=_env_float("REQUEST_TIMEOUT", 30.0),
            session_hours=_env_float("SESSION_HOURS", 12.0),
            cookie_secure=_env_bool("COOKIE_SECURE", False),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            stream_strip_default=_env_bool("STREAM_STRIP_DEFAULT", False),
            poster_refresh_default=_env_bool("POSTER_REFRESH_DEFAULT", False),
            see_all_default=_env_bool("SEE_ALL_DEFAULT", False),
            search_clean_default=_env_bool("SEARCH_CLEAN_DEFAULT", False),
            diagnostics_default=_env_bool("DIAGNOSTICS_DEFAULT", False),
            max_json_mb=int(_env_float("MAX_JSON_MB", 32)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        problems = []
        if not self.username:
            problems.append("REFUSED_USERNAME is not set")
        if not self.password:
            problems.append("REFUSED_PASSWORD is not set")
        elif self.password.upper() == "CHANGE_ME":
            problems.append("REFUSED_PASSWORD is still the CHANGE_ME placeholder")
        if not self.remux_url.startswith(("http://", "https://")):
            problems.append("REMUX_URL must start with http:// or https://")
        if problems:
            raise ConfigError(
                "Refused cannot start: "
                + "; ".join(problems)
                + ". Set them in .env (see .env.example)."
            )
