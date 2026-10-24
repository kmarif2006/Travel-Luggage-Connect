"""
Environment-driven configuration for automatic match notification (Feature 2).

Same style as passenger_auth.config: parsed and bounds-checked once at startup.
"""
import os
from dataclasses import dataclass

from passenger_auth.config import ConfigError


def _float(environ, name, default, lo, hi):
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _int(environ, name, default, lo, hi):
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _choice(environ, name, default, choices):
    value = (environ.get(name) or default).strip().lower()
    if value not in choices:
        raise ConfigError(f"{name} must be one of {sorted(choices)}, got {value!r}")
    return value


def _bool(environ, name, default):
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (1/0/true/false), got {raw!r}")


@dataclass(frozen=True)
class MatchNotifyConfig:
    auto_notify_min: float = 0.85
    review_min: float = 0.60
    hold_days: int = 7
    max_attempts: int = 4
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 1800
    lease_seconds: int = 120
    worker_enabled: bool = True
    worker_interval_seconds: int = 15
    whatsapp_provider: str = "mock"
    email_provider: str = "mock"
    mock_whatsapp_failure: str = "none"
    mock_email_failure: str = "none"
    app_base_url: str = "http://127.0.0.1:5003"
    description_max_chars: int = 80

    def __post_init__(self):
        if not 0.0 <= self.review_min <= self.auto_notify_min <= 1.0:
            raise ConfigError("Require 0 <= MATCH_REVIEW_MIN <= MATCH_AUTO_NOTIFY_MIN <= 1.")

    @classmethod
    def from_env(cls, environ=None) -> "MatchNotifyConfig":
        env = os.environ if environ is None else environ
        base_url = (env.get("APP_BASE_URL") or "http://127.0.0.1:5003").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError("APP_BASE_URL must start with http:// or https://")
        failure_modes = {"none", "transient", "permanent"}
        return cls(
            auto_notify_min=_float(env, "MATCH_AUTO_NOTIFY_MIN", 0.85, 0.0, 1.0),
            review_min=_float(env, "MATCH_REVIEW_MIN", 0.60, 0.0, 1.0),
            hold_days=_int(env, "MATCH_HOLD_DAYS", 7, 1, 90),
            max_attempts=_int(env, "NOTIFY_MAX_ATTEMPTS", 4, 1, 5),
            backoff_base_seconds=_int(env, "NOTIFY_BACKOFF_BASE_SECONDS", 30, 1, 3600),
            backoff_max_seconds=_int(env, "NOTIFY_BACKOFF_MAX_SECONDS", 1800, 1, 86400),
            lease_seconds=_int(env, "NOTIFY_LEASE_SECONDS", 120, 30, 3600),
            worker_enabled=_bool(env, "NOTIFY_WORKER_ENABLED", True),
            worker_interval_seconds=_int(env, "NOTIFY_WORKER_INTERVAL_SECONDS", 15, 1, 3600),
            whatsapp_provider=_choice(env, "WHATSAPP_PROVIDER", "mock", {"mock", "twilio", "meta"}),
            email_provider=_choice(env, "EMAIL_PROVIDER", "mock", {"mock", "smtp"}),
            mock_whatsapp_failure=_choice(env, "MOCK_WHATSAPP_FAILURE", "none", failure_modes),
            mock_email_failure=_choice(env, "MOCK_EMAIL_FAILURE", "none", failure_modes),
            app_base_url=base_url,
        )
