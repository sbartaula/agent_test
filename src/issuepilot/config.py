"""Environment-driven settings. Secrets are only ever read from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_DB_PATH = ".issuepilot/runs.db"
# DeepSeek Flash list prices, USD per 1M tokens (checked against api-docs.deepseek.com).
# Input is the cache-miss price, so cost is a conservative upper bound. Peak hours are
# Mon-Fri 01:00-04:00 and 06:00-10:00 UTC; everything else is off-peak (half price).
PRICES_OFF_PEAK = (0.15, 0.60)
PRICES_PEAK = (0.30, 1.20)


def default_prices(now: datetime | None = None) -> tuple[float, float]:
    now = now or datetime.now(UTC)
    peak = now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10)
    return PRICES_PEAK if peak else PRICES_OFF_PEAK


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)  # repr=False keeps the key out of logs/tracebacks
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    db_path: str = DEFAULT_DB_PATH
    price_input_per_m: float = field(default_factory=lambda: default_prices()[0])
    price_output_per_m: float = field(default_factory=lambda: default_prices()[1])

    @property
    def checkpoint_path(self) -> str:
        return str(Path(self.db_path).with_name("checkpoints.db"))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        key = env.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise ConfigError("DEEPSEEK_API_KEY is not set. See .env.example.")
        try:
            return cls(
                api_key=key,
                base_url=env.get("ISSUEPILOT_BASE_URL", DEFAULT_BASE_URL),
                db_path=env.get("ISSUEPILOT_DB_PATH", DEFAULT_DB_PATH),
                price_input_per_m=float(
                    env.get("ISSUEPILOT_PRICE_INPUT_PER_M", default_prices()[0])
                ),
                price_output_per_m=float(
                    env.get("ISSUEPILOT_PRICE_OUTPUT_PER_M", default_prices()[1])
                ),
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid numeric price setting: {exc}") from exc


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from a local .env without overriding real environment variables."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
