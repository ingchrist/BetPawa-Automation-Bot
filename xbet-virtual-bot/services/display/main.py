"""display — the terminal UI.

A dumb renderer: subscribes to domain events and prints them. It never talks
to 1xbet and never holds business-logic state — swapping it for a Telegram
bot or a web dashboard later means writing a new subscriber, not touching
collector or aggregator.

On startup it replays recent finished results from the aggregator's history
log before switching to live streaming, so a restart doesn't lose the
"what happened earlier" context the live site itself throws away.

Run standalone:
    python -m services.display.main
"""
from __future__ import annotations

import asyncio
import signal

from rich.console import Console

from services.aggregator.history import ResultsHistory
from services.display.render import (
    log_finished,
    render_backfill_header,
    render_discovered,
    render_finished,
    render_half_time,
    render_legend,
    render_live_header,
    render_live_score,
    render_started,
)
from shared.bus import EventBus
from shared.config import load_config
from shared.events import (
    MatchDiscovered,
    MatchFinished,
    MatchHalfTime,
    MatchScoreChanged,
    MatchStarted,
)
from shared.logging import get_logger

BACKFILL_COUNT = 5

# Wide enough that the halves table + a full Total ladder never wrap, but
# fixed rather than following the terminal — data/result.log is meant to be
# opened later for pattern-spotting across many rounds, so every entry
# should look the same regardless of how wide the terminal happened to be
# when the bot was running.
RESULT_LOG_WIDTH = 110


async def run() -> None:
    config = load_config()
    log = get_logger("display", config.log_dir)

    render_legend()

    history = ResultsHistory(config.results_log_path)
    recent = history.load_recent(BACKFILL_COUNT)
    render_backfill_header(len(recent))
    for event in recent:
        render_finished(event, earlier=True)
    render_live_header()

    # Plain-text, human-readable record of every finished round — separate
    # from data/results.jsonl (machine-readable, for the display's own
    # startup backfill) and independent of terminal scrollback. Opened once
    # for the life of the process; only *newly* finished matches get
    # appended (see the MatchFinished branch below) — the backfill replay
    # above is deliberately not logged here, or every restart would
    # duplicate however many rounds BACKFILL_COUNT replays.
    config.result_log_txt_path.parent.mkdir(parents=True, exist_ok=True)
    result_log_file = config.result_log_txt_path.open("a", encoding="utf-8")
    result_log_console = Console(file=result_log_file, no_color=True, width=RESULT_LOG_WIDTH, highlight=False)

    bus = EventBus(config.redis_url)
    await bus.connect()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def consume() -> None:
        async for event in bus.subscribe_match_events(config.channel_match_events):
            try:
                if isinstance(event, MatchDiscovered):
                    render_discovered(event)
                elif isinstance(event, MatchStarted):
                    render_started(event)
                elif isinstance(event, MatchScoreChanged):
                    render_live_score(event)
                elif isinstance(event, MatchHalfTime):
                    render_half_time(event)
                elif isinstance(event, MatchFinished):
                    render_finished(event)
                    log_finished(event, result_log_console)
                    result_log_file.flush()
            except Exception as err:  # noqa: BLE001 — a bad render must not kill the stream
                log.error(f"render failed for {event.kind} (match {event.match_id}): {err}")

    consumer_task = asyncio.create_task(consume())
    await stop.wait()

    log.info("shutting down...")
    consumer_task.cancel()
    result_log_file.close()
    await bus.close()


if __name__ == "__main__":
    asyncio.run(run())
