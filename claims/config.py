"""Environment-driven configuration for secure claims and pickup tokens (Feature 3)."""
import os
from dataclasses import dataclass

from match_notify.config import _float, _int
from passenger_auth.config import ConfigError


@dataclass(frozen=True)
class ClaimsConfig:
    trust_threshold: float = 0.75
    trust_review_min: float = 0.50
    max_rounds: int = 3
    token_ttl_days: int = 7
    photo_url_ttl_seconds: int = 120
    max_claims_per_day: int = 3
    max_answer_submissions_per_day: int = 10
    ip_limit: int = 60
    ip_window_seconds: int = 600
    answer_match_min: float = 0.5
    fuzzy_min_token_length: int = 5
    max_answer_chars: int = 200

    def __post_init__(self):
        if not 0.0 < self.trust_review_min < self.trust_threshold <= 1.0:
            raise ConfigError("Require 0 < TRUST_REVIEW_MIN < TRUST_THRESHOLD <= 1.")

    @classmethod
    def from_env(cls, environ=None) -> "ClaimsConfig":
        env = os.environ if environ is None else environ
        return cls(
            trust_threshold=_float(env, "TRUST_THRESHOLD", 0.75, 0.0, 1.0),
            trust_review_min=_float(env, "TRUST_REVIEW_MIN", 0.50, 0.0, 1.0),
            max_rounds=_int(env, "CLAIM_MAX_ROUNDS", 3, 1, 10),
            token_ttl_days=_int(env, "CLAIM_TOKEN_TTL_DAYS", 7, 1, 60),
            photo_url_ttl_seconds=_int(env, "CLAIM_PHOTO_URL_TTL_SECONDS", 120, 10, 3600),
            max_claims_per_day=_int(env, "CLAIM_MAX_CLAIMS_PER_DAY", 3, 1, 100),
            max_answer_submissions_per_day=_int(env, "CLAIM_MAX_ANSWER_SUBMISSIONS_PER_DAY", 10, 1, 1000),
            ip_limit=_int(env, "CLAIM_IP_LIMIT", 60, 1, 100000),
            ip_window_seconds=_int(env, "CLAIM_IP_WINDOW_SECONDS", 600, 10, 86400),
            answer_match_min=_float(env, "CLAIM_ANSWER_MATCH_MIN", 0.5, 0.1, 1.0),
            fuzzy_min_token_length=_int(env, "CLAIM_FUZZY_MIN_TOKEN_LENGTH", 5, 3, 20),
        )
