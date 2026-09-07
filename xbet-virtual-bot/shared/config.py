"""One frozen config object, built from env vars (+ .env), for every service.

Each service imports `load_config()` and gets the same shape — this is the
only place a knob's name or default is allowed to live.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    api_base: str
    sport_id: int
    league_name_filter: str
    bulk_poll_seconds: float
    detail_poll_seconds: float
    http_timeout_seconds: float
    redis_url: str
    results_log_path: Path
    result_log_txt_path: Path
    log_dir: Path

    # Redis pub/sub channels — the event-bus "contract" shared by every
    # service. Defined here rather than scattered as string literals so a
    # rename can't silently desync a publisher from its subscribers.
    channel_snapshots: str = "xbet.snapshots"
    channel_match_events: str = "xbet.match_events"


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    def _path(env_key: str, default: str) -> Path:
        p = Path(os.environ.get(env_key, default))
        return p if p.is_absolute() else PROJECT_ROOT / p

    return Config(
        api_base=os.environ.get("ONEXBET_API_BASE", "https://1xbet.cm/service-api"),
        sport_id=int(os.environ.get("ONEXBET_SPORT_ID", "85")),
        league_name_filter=os.environ.get("ONEXBET_LEAGUE_NAME_FILTER", "3x3"),
        bulk_poll_seconds=float(os.environ.get("COLLECTOR_BULK_POLL_SECONDS", "5")),
        detail_poll_seconds=float(os.environ.get("COLLECTOR_DETAIL_POLL_SECONDS", "5")),
        http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "10")),
        redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        results_log_path=_path("RESULTS_LOG_PATH", "data/results.jsonl"),
        result_log_txt_path=_path("RESULT_LOG_PATH", "data/result.log"),
        log_dir=_path("LOG_DIR", "logs"),
    )
