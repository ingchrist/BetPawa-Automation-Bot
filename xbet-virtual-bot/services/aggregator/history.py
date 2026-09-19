"""Append-only JSONL log of finished matches.

The live site drops a result from its own UI once it scrolls off "live" —
there is no "yesterday's results" page to re-scrape. This file is this
bot's own memory of that, independent of both the site and of terminal
scrollback, so a past result is recoverable even after the display service
(or the whole bot) has been restarted.
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic import ValidationError

from shared.events import MatchFinished


class ResultsHistory:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: MatchFinished) -> None:
        with self._path.open("a") as f:
            f.write(event.model_dump_json() + "\n")

    def load_recent(self, limit: int, logger: logging.Logger | None = None) -> list[MatchFinished]:
        if not self._path.exists():
            return []
        with self._path.open() as f:
            lines = f.readlines()
        recent = lines[-limit:] if limit else lines
        events = []
        for line in recent:
            if not line.strip():
                continue
            try:
                events.append(MatchFinished.model_validate_json(line))
            except ValidationError:
                # A single interrupted/partial disk write (e.g. a crash mid-append)
                # shouldn't take down the whole backfill — skip just that line.
                if logger is not None:
                    logger.warning(f"skipping unparseable line in {self._path}: {line.strip()[:80]!r}")
        return events
