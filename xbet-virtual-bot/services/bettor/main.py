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

from typing import Awaitable, Callable

from services.bettor.betting_api import (
    BetExecutor,
    BetResult,
    DOUBLE_CHANCE_1X_T,
    DOUBLE_CHANCE_GROUP,
)
from services.bettor.pattern import PatternTracker, RoundPairStreakTracker
from services.bettor.session_watchdog import watchdog_loop
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
from shared.markets import double_chance_winner

PATTERN1_NAME = "1st_half_over_6.5_streak"
PATTERN2_NAME = "2nd_half_under_7.5_streak"
PATTERN3_NAME = "1st_half_winner_2x_streak"
PATTERN4_NAME = "main_game_under_16.5_streak"
PATTERN4_MIN_ODDS = 1.5
PATTERN4_ODDS_POLL_INTERVAL_SECONDS = 5.0


def _market_label(period: int, over: bool, line: float) -> str:
    half = {0: "Main game", 1: "1st half", 2: "2nd half"}[period]
    side = "Over" if over else "Under"
    return f"Total. {half} {side} {line:g}"


def _condition_label(direction: str, period: int, threshold: int, streak_length: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    cmp = "<=" if direction == "at_or_under" else ">="
    return f"{streak_length} consecutive rounds with {half} total {cmp} {threshold}"


def _double_chance_market_label(period: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    return f"Double Chance. {half} 1X"


def _winner_condition_label(streak_length: int) -> str:
    return f"{streak_length} consecutive rounds with 1st half winner 2X"


def _pair_condition_label() -> str:
    return "2-round pair with >=3 of 4 half-totals >= 9"


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

    tracker3 = PatternTracker(
        threshold="2X",
        streak_length=config.pattern3_streak_length,
        direction="equals",
    )
    targets3 = TargetTracker()
    placed_matches3: set[int] = set()

    tracker4 = RoundPairStreakTracker(half_threshold=9, required_count=3)
    targets4 = TargetTracker(stale_statuses={"finished"})
    placed_matches4: set[int] = set()
    # place() is spawned as a task (not awaited) for Pattern 4 only, since
    # its odds-wait can span an entire match -- these references keep the
    # tasks from being garbage-collected mid-flight (a real asyncio
    # requirement, not optional bookkeeping).
    pattern4_tasks: list[asyncio.Task] = []

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
    log.info(
        f"starting Pattern 3 — streak_length={config.pattern3_streak_length} "
        f"trigger=2X selection=1X stake={config.pattern3_bet_stake_amount} "
        f"{'ENABLED' if config.pattern3_enabled else 'DISABLED (PATTERN3_ENABLED=false) — tracking only, will not bet'}"
    )
    log.info(
        f"starting Pattern 4 — half_threshold=9 required_count=3(of 4) "
        f"bet_line={config.pattern4_bet_line} min_odds={PATTERN4_MIN_ODDS} "
        f"stake={config.pattern4_bet_stake_amount} "
        f"{'ENABLED' if config.pattern4_enabled else 'DISABLED (PATTERN4_ENABLED=false) — tracking only, will not bet'}"
    )
    log.info(
        f"starting session watchdog — check_interval={config.auth_watchdog_check_interval_seconds:g}s "
        f"cooldown={config.auth_watchdog_login_cooldown_seconds:g}s "
        f"{'ENABLED' if config.auth_watchdog_enabled else 'DISABLED (AUTH_WATCHDOG_ENABLED=false)'}"
    )

    def _log_pattern4_task_exception(task: asyncio.Task) -> None:
        """A bare fire-and-forget asyncio.Task otherwise swallows an
        unexpected exception silently -- every other pattern's failures
        already surface via BetFailed/log lines, so this one should too."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"pattern4 placement task failed: {exc}")

    async def place(
        target: MatchDiscovered,
        *,
        targets: TargetTracker,
        other_patterns: list[tuple[TargetTracker, str]],
        placed_matches: set[int],
        market_label: str,
        stale_period_label: str,
        line: float | None,
        stake: float,
        place_call: Callable[[], Awaitable[BetResult]],
    ) -> None:
        for other_targets, other_pattern_name in other_patterns:
            conflict = mutual_exclusion_reason(target.match_id, other_pattern_name, other_targets.bet_targets)
            if conflict is not None:
                log.warning(conflict)
                await bus.publish(
                    config.channel_match_events,
                    BetFailed(match_id=target.match_id, reason=conflict, market_label=market_label),
                )
                return

        if targets.is_stale(target.match_id):
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                f"already past its {stale_period_label} by the time the bet was attempted"
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
        result = await place_call()
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
                            other_patterns=[(targets2, PATTERN2_NAME), (targets3, PATTERN3_NAME)],
                            placed_matches=placed_matches,
                            market_label=_market_label(1, True, config.pattern_bet_line),
                            stale_period_label="1st half",
                            line=config.pattern_bet_line,
                            stake=config.bet_stake_amount,
                            place_call=lambda: executor.place_bet(
                                match_id=target.match_id,
                                home=target.home,
                                away=target.away,
                                stake=config.bet_stake_amount,
                                line=config.pattern_bet_line,
                                period=1,
                                over=True,
                            ),
                        )
                    target2 = targets2.on_discovered(event)
                    if target2 is not None and config.pattern2_enabled:
                        await place(
                            target2,
                            targets=targets2,
                            other_patterns=[(targets, PATTERN1_NAME), (targets3, PATTERN3_NAME)],
                            placed_matches=placed_matches2,
                            market_label=_market_label(2, False, config.pattern2_bet_line),
                            stale_period_label="2nd half",
                            line=config.pattern2_bet_line,
                            stake=config.pattern2_bet_stake_amount,
                            place_call=lambda: executor.place_bet(
                                match_id=target2.match_id,
                                home=target2.home,
                                away=target2.away,
                                stake=config.pattern2_bet_stake_amount,
                                line=config.pattern2_bet_line,
                                period=2,
                                over=False,
                            ),
                        )
                    target3 = targets3.on_discovered(event)
                    if target3 is not None and config.pattern3_enabled:
                        await place(
                            target3,
                            targets=targets3,
                            other_patterns=[(targets, PATTERN1_NAME), (targets2, PATTERN2_NAME)],
                            placed_matches=placed_matches3,
                            market_label=_double_chance_market_label(1),
                            stale_period_label="1st half",
                            line=None,
                            stake=config.pattern3_bet_stake_amount,
                            place_call=lambda: executor.place_bet(
                                match_id=target3.match_id,
                                home=target3.home,
                                away=target3.away,
                                stake=config.pattern3_bet_stake_amount,
                                line=None,
                                period=1,
                                bet_type=DOUBLE_CHANCE_1X_T,
                                group=DOUBLE_CHANCE_GROUP,
                            ),
                        )
                    target4 = targets4.on_discovered(event)
                    if target4 is not None and config.pattern4_enabled:
                        task = asyncio.create_task(
                            place(
                                target4,
                                targets=targets4,
                                other_patterns=[],
                                placed_matches=placed_matches4,
                                market_label=_market_label(0, False, config.pattern4_bet_line),
                                stale_period_label="match end",
                                line=config.pattern4_bet_line,
                                stake=config.pattern4_bet_stake_amount,
                                place_call=lambda: executor.place_bet(
                                    match_id=target4.match_id,
                                    home=target4.home,
                                    away=target4.away,
                                    stake=config.pattern4_bet_stake_amount,
                                    line=config.pattern4_bet_line,
                                    period=0,
                                    over=False,
                                    min_odds=PATTERN4_MIN_ODDS,
                                    poll_interval_seconds=PATTERN4_ODDS_POLL_INTERVAL_SECONDS,
                                    is_stale=lambda: targets4.is_stale(target4.match_id),
                                ),
                            )
                        )
                        pattern4_tasks.append(task)
                        task.add_done_callback(_log_pattern4_task_exception)
                elif isinstance(event, MatchStarted):
                    targets.on_started(event.match_id)
                    targets2.on_started(event.match_id)
                    targets3.on_started(event.match_id)
                    targets4.on_started(event.match_id)
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                    targets2.on_half_time(event.match_id)
                    targets3.on_half_time(event.match_id)
                    targets4.on_half_time(event.match_id)

                    first_half_total = event.first_half.home_goals + event.first_half.away_goals
                    winner_1h = double_chance_winner(event.first_half.home_goals, event.first_half.away_goals)

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

                    if event.match_id in placed_matches3:
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=(winner_1h != "2X"),
                                period_total=first_half_total,
                                market_label=_double_chance_market_label(1),
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
                                other_patterns=[(targets2, PATTERN2_NAME), (targets3, PATTERN3_NAME)],
                                placed_matches=placed_matches,
                                market_label=_market_label(1, True, config.pattern_bet_line),
                                stale_period_label="1st half",
                                line=config.pattern_bet_line,
                                stake=config.bet_stake_amount,
                                place_call=lambda: executor.place_bet(
                                    match_id=target.match_id,
                                    home=target.home,
                                    away=target.away,
                                    stake=config.bet_stake_amount,
                                    line=config.pattern_bet_line,
                                    period=1,
                                    over=True,
                                ),
                            )
                        else:
                            log.info(f"PATTERN 1 fired but no target yet — {targets.debug_state()}")
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

                    fired3 = tracker3.process(winner_1h)
                    if fired3:
                        if not config.pattern3_enabled:
                            log.warning(
                                f"PATTERN 3 ARMED — streak {tracker3.last_streak_totals} "
                                "— but PATTERN3_ENABLED=false, suppressing bet placement"
                            )
                        else:
                            log.info(f"PATTERN 3 ARMED — streak {tracker3.last_streak_totals}")
                            await bus.publish(
                                config.channel_match_events,
                                PatternArmed(
                                    pattern_name=PATTERN3_NAME,
                                    qualifying_totals=list(tracker3.last_streak_totals),
                                    market_label=_double_chance_market_label(1),
                                    condition_label=_winner_condition_label(config.pattern3_streak_length),
                                ),
                            )
                            target3 = targets3.arm()
                            if target3 is not None:
                                await place(
                                    target3,
                                    targets=targets3,
                                    other_patterns=[(targets, PATTERN1_NAME), (targets2, PATTERN2_NAME)],
                                    placed_matches=placed_matches3,
                                    market_label=_double_chance_market_label(1),
                                    stale_period_label="1st half",
                                    line=None,
                                    stake=config.pattern3_bet_stake_amount,
                                    place_call=lambda: executor.place_bet(
                                        match_id=target3.match_id,
                                        home=target3.home,
                                        away=target3.away,
                                        stake=config.pattern3_bet_stake_amount,
                                        line=None,
                                        period=1,
                                        bet_type=DOUBLE_CHANCE_1X_T,
                                        group=DOUBLE_CHANCE_GROUP,
                                    ),
                                )
                            else:
                                log.info(f"PATTERN 3 fired but no target yet — {targets3.debug_state()}")
                    else:
                        await bus.publish(
                            config.channel_match_events,
                            PatternProgress(
                                match_id=event.match_id,
                                pattern_name=PATTERN3_NAME,
                                direction="equals",
                                streak=tracker3.streak,
                                streak_length=config.pattern3_streak_length,
                                threshold="2X",
                                total=tracker3.last_total,
                                outcome=tracker3.last_outcome,
                            ),
                        )
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)
                    targets2.on_finished(event.match_id)
                    targets3.on_finished(event.match_id)
                    targets4.on_finished(event.match_id)

                    second_half_total = (
                        None if event.second_half is None
                        else event.second_half.home_goals + event.second_half.away_goals
                    )
                    first_half_total = (
                        None if event.first_half is None
                        else event.first_half.home_goals + event.first_half.away_goals
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
                                    other_patterns=[(targets, PATTERN1_NAME), (targets3, PATTERN3_NAME)],
                                    placed_matches=placed_matches2,
                                    market_label=_market_label(2, False, config.pattern2_bet_line),
                                    stale_period_label="2nd half",
                                    line=config.pattern2_bet_line,
                                    stake=config.pattern2_bet_stake_amount,
                                    place_call=lambda: executor.place_bet(
                                        match_id=target2.match_id,
                                        home=target2.home,
                                        away=target2.away,
                                        stake=config.pattern2_bet_stake_amount,
                                        line=config.pattern2_bet_line,
                                        period=2,
                                        over=False,
                                    ),
                                )
                            else:
                                log.info(f"PATTERN 2 fired but no target yet — {targets2.debug_state()}")
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

                    if event.match_id in placed_matches4:
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=event.total_goals < config.pattern4_bet_line,
                                period_total=event.total_goals,
                                market_label=_market_label(0, False, config.pattern4_bet_line),
                            ),
                        )

                    fired4 = tracker4.process(first_half_total, second_half_total)
                    if fired4:
                        if not config.pattern4_enabled:
                            log.warning(
                                f"PATTERN 4 ARMED — pair values {tracker4.last_pair_values} "
                                "— but PATTERN4_ENABLED=false, suppressing bet placement"
                            )
                        else:
                            log.info(f"PATTERN 4 ARMED — pair values {tracker4.last_pair_values}")
                            await bus.publish(
                                config.channel_match_events,
                                PatternArmed(
                                    pattern_name=PATTERN4_NAME,
                                    qualifying_totals=list(tracker4.last_pair_values),
                                    market_label=_market_label(0, False, config.pattern4_bet_line),
                                    condition_label=_pair_condition_label(),
                                ),
                            )
                            target4 = targets4.arm()
                            if target4 is not None:
                                task = asyncio.create_task(
                                    place(
                                        target4,
                                        targets=targets4,
                                        other_patterns=[],
                                        placed_matches=placed_matches4,
                                        market_label=_market_label(0, False, config.pattern4_bet_line),
                                        stale_period_label="match end",
                                        line=config.pattern4_bet_line,
                                        stake=config.pattern4_bet_stake_amount,
                                        place_call=lambda: executor.place_bet(
                                            match_id=target4.match_id,
                                            home=target4.home,
                                            away=target4.away,
                                            stake=config.pattern4_bet_stake_amount,
                                            line=config.pattern4_bet_line,
                                            period=0,
                                            over=False,
                                            min_odds=PATTERN4_MIN_ODDS,
                                            poll_interval_seconds=PATTERN4_ODDS_POLL_INTERVAL_SECONDS,
                                            is_stale=lambda: targets4.is_stale(target4.match_id),
                                        ),
                                    )
                                )
                                pattern4_tasks.append(task)
                                task.add_done_callback(_log_pattern4_task_exception)
                            else:
                                log.info(f"PATTERN 4 fired but no target yet — {targets4.debug_state()}")
                    else:
                        await bus.publish(
                            config.channel_match_events,
                            PatternProgress(
                                match_id=event.match_id,
                                pattern_name=PATTERN4_NAME,
                                direction="pair_count",
                                streak=tracker4.rounds_in_pair,
                                streak_length=2,
                                threshold=9,
                                total=tracker4.last_qualifying_count,
                                outcome=tracker4.last_outcome,
                            ),
                        )
            except Exception as err:  # noqa: BLE001 — one bad event must not kill the subscription
                log.error(f"failed to process {event.kind}: {err}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    consumer_task = asyncio.create_task(consume())
    watchdog_task = (
        asyncio.create_task(
            watchdog_loop(
                config.cdp_url,
                config.onexbet_phone_number,
                config.onexbet_password,
                config.auth_watchdog_check_interval_seconds,
                config.auth_watchdog_login_cooldown_seconds,
                config.auth_watchdog_login_timeout_seconds,
                log,
            )
        )
        if config.auth_watchdog_enabled
        else None
    )
    await stop.wait()

    log.info("shutting down...")
    consumer_task.cancel()
    if watchdog_task is not None:
        watchdog_task.cancel()
    for task in pattern4_tasks:
        if not task.done():
            task.cancel()
    await executor.aclose()
    await bus.close()


if __name__ == "__main__":
    asyncio.run(run())
