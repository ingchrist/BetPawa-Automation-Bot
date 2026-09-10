"""bettor — Pattern 1: "1st Half Over 6.5" streak detector + live bet
placement.

Subscribes to xbet.match_events like display does, feeds every finished
round's 1st-half total into PatternTracker, resolves the bet target via
TargetTracker, and drives BetExecutor to actually place it. Publishes
PatternArmed/BetPlaced/BetFailed/BetSettled back onto the same channel —
just another producer on the existing bus, same shape as the aggregator.

Run standalone:
    python -m services.bettor.main
"""
from __future__ import annotations

import asyncio
import signal

from services.bettor.betting_api import BetExecutor
from services.bettor.pattern import PatternTracker
from services.bettor.targeting import TargetTracker
from shared.bus import EventBus
from shared.config import load_config
from shared.events import (
    BetFailed,
    BetPlaced,
    BetSettled,
    MatchDiscovered,
    MatchFinished,
    MatchHalfTime,
    MatchStarted,
    PatternArmed,
)
from shared.logging import get_logger


def _first_half_total(event: MatchFinished) -> int | None:
    if event.first_half is None:
        return None
    return event.first_half.home_goals + event.first_half.away_goals


async def run() -> None:
    config = load_config()
    log = get_logger("bettor", config.log_dir)

    bus = EventBus(config.redis_url)
    await bus.connect()

    tracker = PatternTracker(low_threshold=config.pattern_low_threshold, streak_length=config.pattern_streak_length)
    targets = TargetTracker()
    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )

    async def place(target: MatchDiscovered) -> None:
        if targets.is_stale(target.match_id):
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                "already past 1st half by the time the bet was attempted"
            )
            log.warning(reason)
            await bus.publish(config.channel_match_events, BetFailed(match_id=target.match_id, reason=reason))
            return

        log.info(
            f"placing bet: {config.bet_stake_amount:g} on {target.home} vs {target.away} "
            f"(match {target.match_id}) Total. 1st half Over {config.pattern_bet_line:g}"
        )
        result = await executor.place_bet(
            match_id=target.match_id,
            home=target.home,
            away=target.away,
            stake=config.bet_stake_amount,
            line=config.pattern_bet_line,
        )
        if result.success:
            log.info(f"bet placed on match {target.match_id} at odds {result.odds}")
            await bus.publish(
                config.channel_match_events,
                BetPlaced(
                    match_id=target.match_id,
                    league_name=target.league_name,
                    home=target.home,
                    away=target.away,
                    stake=config.bet_stake_amount,
                    line=config.pattern_bet_line,
                    odds=result.odds,
                ),
            )
        else:
            log.error(f"bet failed for match {target.match_id}: {result.reason}")
            await bus.publish(config.channel_match_events, BetFailed(match_id=target.match_id, reason=result.reason))

    async def consume() -> None:
        async for event in bus.subscribe_match_events(config.channel_match_events):
            try:
                if isinstance(event, MatchDiscovered):
                    target = targets.on_discovered(event)
                    if target is not None:
                        await place(target)
                elif isinstance(event, MatchStarted):
                    targets.on_started(event.match_id)
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)

                    if event.match_id in targets.bet_targets and event.first_half is not None:
                        first_half_total = event.first_half.home_goals + event.first_half.away_goals
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=first_half_total > config.pattern_bet_line,
                                first_half_total=first_half_total,
                            ),
                        )

                    fired = tracker.process(_first_half_total(event))
                    if fired:
                        log.info(f"PATTERN ARMED — streak {tracker.last_streak_totals}")
                        await bus.publish(
                            config.channel_match_events,
                            PatternArmed(
                                pattern_name="1st_half_over_6.5_streak",
                                qualifying_totals=list(tracker.last_streak_totals),
                            ),
                        )
                        target = targets.arm()
                        if target is not None:
                            await place(target)
            except Exception as err:  # noqa: BLE001 — one bad event must not kill the subscription
                log.error(f"failed to process {event.kind}: {err}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    consumer_task = asyncio.create_task(consume())
    await stop.wait()

    log.info("shutting down...")
    consumer_task.cancel()
    await executor.aclose()
    await bus.close()


if __name__ == "__main__":
    asyncio.run(run())
