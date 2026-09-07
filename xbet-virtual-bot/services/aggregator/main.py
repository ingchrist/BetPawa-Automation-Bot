"""aggregator — the brain.

Subscribes to raw snapshots from the collector, runs them through the state
machine, publishes named domain events for anything downstream to consume,
and durably logs every finished result. Knows nothing about terminals, ANSI
colors, or how anything gets displayed — that's the display service's job.

Run standalone:
    python -m services.aggregator.main
"""
from __future__ import annotations

import asyncio
import signal

from services.aggregator.history import ResultsHistory
from services.aggregator.state import MatchStateMachine
from shared.bus import EventBus
from shared.config import load_config
from shared.events import MatchFinished
from shared.logging import get_logger


async def run() -> None:
    config = load_config()
    log = get_logger("aggregator", config.log_dir)

    bus = EventBus(config.redis_url)
    await bus.connect()

    history = ResultsHistory(config.results_log_path)
    machine = MatchStateMachine()

    log.info(f"starting — results log at {config.results_log_path}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def consume() -> None:
        async for snapshot in bus.subscribe_snapshots(config.channel_snapshots):
            try:
                for event in machine.process(snapshot):
                    await bus.publish(config.channel_match_events, event)
                    if isinstance(event, MatchFinished):
                        history.append(event)
                        log.info(
                            f"FINISHED {event.home} {event.total_home_goals}-{event.total_away_goals} {event.away} "
                            f"(match {event.match_id})"
                        )
                    else:
                        log.info(f"{event.kind} — match {event.match_id} ({event.home} vs {event.away})")
            except Exception as err:  # noqa: BLE001 — one bad snapshot must not kill the subscription
                log.error(f"failed to process snapshot for match {snapshot.match_id}: {err}")

    consumer_task = asyncio.create_task(consume())
    await stop.wait()

    log.info("shutting down...")
    consumer_task.cancel()
    await bus.close()


if __name__ == "__main__":
    asyncio.run(run())
