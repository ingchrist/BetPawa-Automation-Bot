"""TargetTracker — resolves which upcoming match a fired pattern should
bet on, and guards against betting into a match that's already past this
instance's configured stale statuses by the time the bet is actually
attempted.

Pure, no I/O — fed only the MatchDiscovered/MatchStarted/MatchHalfTime/
MatchFinished events services/bettor/main.py already subscribes to. The
"single announced next match" semantics this relies on are the
aggregator's own (see _update_upcoming_queue in services/aggregator/
state.py) — this module just tracks the latest one it's seen.
"""
from __future__ import annotations

from shared.events import MatchDiscovered

_DEFAULT_STALE_STATUSES = {"half_time", "finished"}


class TargetTracker:
    def __init__(self, stale_statuses: set[str] | None = None) -> None:
        """`stale_statuses` controls which match statuses this instance
        treats as "too late to bet" — default is Pattern 1's shape (the
        1st-half market closes at half-time). Pattern 2 passes
        `{"finished"}` since half-time is exactly when its 2nd-half
        market *opens*, not when it closes."""
        self._latest_discovered: MatchDiscovered | None = None
        self._status: dict[int, str] = {}
        self._pending = False
        self._stale_statuses = set(stale_statuses) if stale_statuses is not None else set(_DEFAULT_STALE_STATUSES)
        self.bet_targets: set[int] = set()

    def on_discovered(self, event: MatchDiscovered) -> MatchDiscovered | None:
        """Update the latest known "next" match. If a fire is waiting for
        a target (pending), this discovered match becomes it and is
        returned for immediate betting."""
        self._latest_discovered = event
        self._status[event.match_id] = "upcoming"
        if self._pending and event.match_id not in self.bet_targets:
            self._pending = False
            self.bet_targets.add(event.match_id)
            return event
        return None

    def on_started(self, match_id: int) -> None:
        self._status[match_id] = "started"

    def on_half_time(self, match_id: int) -> None:
        self._status[match_id] = "half_time"

    def on_finished(self, match_id: int) -> None:
        self._status[match_id] = "finished"

    def arm(self) -> MatchDiscovered | None:
        """Called when the pattern fires. Returns the match to bet on now
        if it's already known, not yet targeted, and not yet stale per
        this instance's stale_statuses (i.e. still upcoming, or already
        started but not yet in one of those statuses) — pregame and
        early-live are both fair game, since BetExecutor prices the bet
        fresh either way and the stale-fire guard in place()/is_stale()
        still catches anything that reaches a stale status. Otherwise
        marks that a bet is owed to whichever match is discovered next."""
        latest = self._latest_discovered
        if (
            latest is not None
            and latest.match_id not in self.bet_targets
            and self._status.get(latest.match_id) not in self._stale_statuses
        ):
            self.bet_targets.add(latest.match_id)
            return latest
        self._pending = True
        return None

    def is_stale(self, match_id: int) -> bool:
        """True if this match's status is already one of this instance's
        configured stale_statuses — checked immediately before actually
        clicking a bet, since real wall-clock time passes during browser
        navigation while this tracker keeps receiving events in the
        background."""
        return self._status.get(match_id) in self._stale_statuses


def mutual_exclusion_reason(
    match_id: int, other_pattern_name: str, other_bet_targets: set[int]
) -> str | None:
    """A ready-to-publish BetFailed reason when `match_id` is already
    targeted by a *different* pattern's TargetTracker -- callers check
    this immediately before invoking BetExecutor, so two independent
    patterns never both stake money on the same match in the same round.
    Returns None when there's no conflict, i.e. the bet should proceed.
    Deliberately a plain function, not a TargetTracker method -- keeps
    the class itself pattern-agnostic, unaware that a second pattern
    even exists."""
    if match_id in other_bet_targets:
        return f"mutual exclusion: match {match_id} already targeted by {other_pattern_name}"
    return None
