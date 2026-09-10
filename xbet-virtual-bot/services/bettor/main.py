"""bettor — Pattern 1: "1st Half Over 6.5" streak detector + live bet
placement.

Subscribes to xbet.match_events like display does, feeds every round's
1st-half total into PatternTracker the moment it's known — at
MatchHalfTime, not MatchFinished, since the pattern only ever looks at
1st-half data and MatchHalfTime.first_half is available well before the
2nd half (and hence MatchFinished) plays out. Waiting for MatchFinished
would delay pattern firing by a full 2nd half's worth of wall-clock time,
which is exactly the lag that let the real "next match to kick off"
(the aggregator's own soonest-upcoming pick — see
services/aggregator/state.py's _update_upcoming_queue) slip from
"upcoming" to "started" before TargetTracker.arm() ever got a chance to
target it.

Resolves the bet target via TargetTracker, and drives BetExecutor to
actually place it. Publishes PatternArmed/PatternProgress/BetPlaced/
BetFailed/BetSettled back onto the same channel — just another producer
on the existing bus, same shape as the aggregator.

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
    PatternProgress,
)
from shared.logging import get_logger

PATTERN1_NAME = "1st_half_over_6.5_streak"


def _market_label(period: int, over: bool, line: float) -> str:
    half = "1st half" if period == 1 else "2nd half"
    side = "Over" if over else "Under"
    return f"Total. {half} {side} {line:g}"


def _condition_label(direction: str, period: int, threshold: int, streak_length: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    cmp = "<=" if direction == "at_or_under" else ">="
    return f"{streak_length} consecutive rounds with {half} total {cmp} {threshold}"


async def run() -> None:
    config = load_config()
    log = get_logger("bettor", config.log_dir)

    bus = EventBus(config.redis_url)
    await bus.connect()

    tracker = PatternTracker(threshold=config.pattern_low_threshold, streak_length=config.pattern_streak_length)
    targets = TargetTracker()
    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )

    async def place(target: MatchDiscovered) -> None:
        market_label = _market_label(1, True, config.pattern_bet_line)

        if targets.is_stale(target.match_id):
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                "already past 1st half by the time the bet was attempted"
            )
            log.warning(reason)
            await bus.publish(
                config.channel_match_events,
                BetFailed(match_id=target.match_id, reason=reason, market_label=market_label),
            )
            return

        log.info(
            f"placing bet: {config.bet_stake_amount:g} on {target.home} vs {target.away} "
            f"(match {target.match_id}) {market_label}"
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
                    market_label=market_label,
                    odds=result.odds,
                ),
            )
        else:
            log.error(f"bet failed for match {target.match_id}: {result.reason}")
            await bus.publish(
                config.channel_match_events,
                BetFailed(match_id=target.match_id, reason=result.reason, market_label=market_label),
            )

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

                    first_half_total = event.first_half.home_goals + event.first_half.away_goals

                    if event.match_id in targets.bet_targets:
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=first_half_total > config.pattern_bet_line,
                                period_total=first_half_total,
                                market_label=_market_label(1, True, config.pattern_bet_line),
                            ),
                        )

                    fired = tracker.process(first_half_total)
                    if fired:
                        log.info(f"PATTERN ARMED — streak {tracker.last_streak_totals}")
                        await bus.publish(
                            config.channel_match_events,
                            PatternArmed(
                                pattern_name=PATTERN1_NAME,
                                qualifying_totals=list(tracker.last_streak_totals),
                                market_label=_market_label(1, True, config.pattern_bet_line),
                                condition_label=_condition_label(
                                    "at_or_under", 1, config.pattern_low_threshold, config.pattern_streak_length
                                ),
                            ),
                        )
                        target = targets.arm()
                        if target is not None:
                            await place(target)
                    else:
                        await bus.publish(
                            config.channel_match_events,
                            PatternProgress(
                                match_id=event.match_id,
                                pattern_name=PATTERN1_NAME,
                                direction="at_or_under",
                                streak=tracker.streak,
                                streak_length=config.pattern_streak_length,
                                threshold=config.pattern_low_threshold,
                                total=tracker.last_total,
                                outcome=tracker.last_outcome,
                            ),
                        )
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)
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
