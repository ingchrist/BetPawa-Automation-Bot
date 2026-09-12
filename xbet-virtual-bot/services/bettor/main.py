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
from services.bettor.targeting import TargetTracker, mutual_exclusion_reason
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
PATTERN2_NAME = "2nd_half_under_7.5_streak"


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
    # Separate from targets.bet_targets: bet_targets means "already targeted,
    # don't retarget" (needed by dedup/mutual-exclusion the moment a target is
    # resolved). placed_matches means "a bet was actually placed" (populated
    # only once place() gets a successful BetPlaced) -- the two are not the
    # same thing whenever place() bails out on mutual exclusion or a stale-fire
    # guard after the match_id is already in bet_targets. The BetSettled gates
    # below need the latter, not the former, or they fabricate a settlement
    # for a bet that was never placed.
    placed_matches: set[int] = set()

    tracker2 = PatternTracker(
        threshold=config.pattern2_high_threshold,
        streak_length=config.pattern2_streak_length,
        direction="at_or_over",
    )
    targets2 = TargetTracker(stale_statuses={"finished"})
    placed_matches2: set[int] = set()

    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting Pattern 1 — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )
    log.info(
        f"starting Pattern 2 — streak_length={config.pattern2_streak_length} "
        f"high_threshold={config.pattern2_high_threshold} bet_line={config.pattern2_bet_line} "
        f"stake={config.pattern2_bet_stake_amount} "
        f"{'ENABLED' if config.pattern2_enabled else 'DISABLED (PATTERN2_ENABLED=false) — tracking only, will not bet'}"
    )

    async def place(
        target: MatchDiscovered,
        *,
        targets: TargetTracker,
        other_targets: TargetTracker,
        other_pattern_name: str,
        placed_matches: set[int],
        period: int,
        over: bool,
        line: float,
        stake: float,
    ) -> None:
        market_label = _market_label(period, over, line)

        conflict = mutual_exclusion_reason(target.match_id, other_pattern_name, other_targets.bet_targets)
        if conflict is not None:
            log.warning(conflict)
            await bus.publish(
                config.channel_match_events,
                BetFailed(match_id=target.match_id, reason=conflict, market_label=market_label),
            )
            return

        if targets.is_stale(target.match_id):
            half = "1st half" if period == 1 else "2nd half"
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                f"already past its {half} by the time the bet was attempted"
            )
            log.warning(reason)
            await bus.publish(
                config.channel_match_events,
                BetFailed(match_id=target.match_id, reason=reason, market_label=market_label),
            )
            return

        log.info(
            f"placing bet: {stake:g} on {target.home} vs {target.away} "
            f"(match {target.match_id}) {market_label}"
        )
        result = await executor.place_bet(
            match_id=target.match_id,
            home=target.home,
            away=target.away,
            stake=stake,
            line=line,
            period=period,
            over=over,
        )
        if result.success:
            log.info(f"bet placed on match {target.match_id} at odds {result.odds}")
            placed_matches.add(target.match_id)
            await bus.publish(
                config.channel_match_events,
                BetPlaced(
                    match_id=target.match_id,
                    league_name=target.league_name,
                    home=target.home,
                    away=target.away,
                    stake=stake,
                    line=line,
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
                        await place(
                            target,
                            targets=targets,
                            other_targets=targets2,
                            other_pattern_name=PATTERN2_NAME,
                            placed_matches=placed_matches,
                            period=1,
                            over=True,
                            line=config.pattern_bet_line,
                            stake=config.bet_stake_amount,
                        )
                    target2 = targets2.on_discovered(event)
                    if target2 is not None and config.pattern2_enabled:
                        await place(
                            target2,
                            targets=targets2,
                            other_targets=targets,
                            other_pattern_name=PATTERN1_NAME,
                            placed_matches=placed_matches2,
                            period=2,
                            over=False,
                            line=config.pattern2_bet_line,
                            stake=config.pattern2_bet_stake_amount,
                        )
                elif isinstance(event, MatchStarted):
                    targets.on_started(event.match_id)
                    targets2.on_started(event.match_id)
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                    targets2.on_half_time(event.match_id)

                    first_half_total = event.first_half.home_goals + event.first_half.away_goals

                    if event.match_id in placed_matches:
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
                        log.info(f"PATTERN 1 ARMED — streak {tracker.last_streak_totals}")
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
                            await place(
                                target,
                                targets=targets,
                                other_targets=targets2,
                                other_pattern_name=PATTERN2_NAME,
                                placed_matches=placed_matches,
                                period=1,
                                over=True,
                                line=config.pattern_bet_line,
                                stake=config.bet_stake_amount,
                            )
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
                    targets2.on_finished(event.match_id)

                    second_half_total = (
                        None if event.second_half is None
                        else event.second_half.home_goals + event.second_half.away_goals
                    )

                    if event.match_id in placed_matches2 and second_half_total is not None:
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=second_half_total < config.pattern2_bet_line,
                                period_total=second_half_total,
                                market_label=_market_label(2, False, config.pattern2_bet_line),
                            ),
                        )

                    fired2 = tracker2.process(second_half_total)
                    if fired2:
                        if not config.pattern2_enabled:
                            log.warning(
                                f"PATTERN 2 ARMED — streak {tracker2.last_streak_totals} "
                                "— but PATTERN2_ENABLED=false, suppressing bet placement"
                            )
                        else:
                            log.info(f"PATTERN 2 ARMED — streak {tracker2.last_streak_totals}")
                            await bus.publish(
                                config.channel_match_events,
                                PatternArmed(
                                    pattern_name=PATTERN2_NAME,
                                    qualifying_totals=list(tracker2.last_streak_totals),
                                    market_label=_market_label(2, False, config.pattern2_bet_line),
                                    condition_label=_condition_label(
                                        "at_or_over", 2, config.pattern2_high_threshold, config.pattern2_streak_length
                                    ),
                                ),
                            )
                            target2 = targets2.arm()
                            if target2 is not None:
                                await place(
                                    target2,
                                    targets=targets2,
                                    other_targets=targets,
                                    other_pattern_name=PATTERN1_NAME,
                                    placed_matches=placed_matches2,
                                    period=2,
                                    over=False,
                                    line=config.pattern2_bet_line,
                                    stake=config.pattern2_bet_stake_amount,
                                )
                    else:
                        await bus.publish(
                            config.channel_match_events,
                            PatternProgress(
                                match_id=event.match_id,
                                pattern_name=PATTERN2_NAME,
                                direction="at_or_over",
                                streak=tracker2.streak,
                                streak_length=config.pattern2_streak_length,
                                threshold=config.pattern2_high_threshold,
                                total=tracker2.last_total,
                                outcome=tracker2.last_outcome,
                            ),
                        )
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
