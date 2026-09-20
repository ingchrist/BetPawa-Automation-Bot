"""TargetTracker — resolves which upcoming match a fired pattern should
bet on, and guards against betting into a match that's already past this
instance's configured stale statuses by the time the bet is actually
attempted.

Pure, no I/O — fed only the MatchDiscovered/MatchStarted/MatchHalfTime/
MatchFinished events services/bettor/main.py already subscribes to. The
"single announced next match" semantics come from the aggregator (see
_update_upcoming_queue in services/aggregator/state.py) — but that
announcement is re-issued (superseding the previous one) the instant the
previously-announced match stops being "upcoming", i.e. the moment it
actually kicks off. This module therefore keeps every announcement it's
seen, in discovery order (which is always kickoff order — the aggregator
only ever announces the current soonest match), rather than only the
latest one. `arm()` resolves to the OLDEST still-viable entry, not the
newest: a pattern that fires live, mid-round (Patterns 4/5), or any time
after `place()` has some async delay, can easily run after the real next
round has already gone live *and* a further-out round has already been
freshly announced behind it — the real next round must still win, since
"started" alone doesn't make a tracker's own configured stale_statuses
consider it too late to bet.
"""
from __future__ import annotations

from collections import deque

from shared.events import MatchDiscovered

_DEFAULT_STALE_STATUSES = {"half_time", "finished"}


class TargetTracker:
    def __init__(self, stale_statuses: set[str] | None = None) -> None:
        """`stale_statuses` controls which match statuses this instance
        treats as "too late to bet" — default is Pattern 1's shape (the
        1st-half market closes at half-time). Pattern 2 passes
        `{"finished"}` since half-time is exactly when its 2nd-half
        market *opens*, not when it closes."""
        self._queue: deque[MatchDiscovered] = deque()
        self._status: dict[int, str] = {}
        self._pending = False
        self._stale_statuses = set(stale_statuses) if stale_statuses is not None else set(_DEFAULT_STALE_STATUSES)
        self.bet_targets: set[int] = set()

    def on_discovered(self, event: MatchDiscovered) -> MatchDiscovered | None:
        """Record this announcement (oldest-first queue, never
        overwriting an earlier still-viable one). If a fire is waiting
        for a target (pending), resolve it now to the oldest viable
        queued announcement and return it for immediate betting."""
        self._status[event.match_id] = "upcoming"
        self._queue.append(event)
        if self._pending:
            candidate = self._next_viable_candidate()
            if candidate is not None:
                self._pending = False
                self.bet_targets.add(candidate.match_id)
                return candidate
        return None

    def _next_viable_candidate(self) -> MatchDiscovered | None:
        """Drops queued announcements that are no longer viable targets
        (already bet on, or already past this instance's stale_statuses
        — genuinely too late, not merely started) and returns the oldest
        one that's left, without consuming it. Matches queued behind a
        still-viable one are left untouched — they only become
        candidates once everything ahead of them is exhausted."""
        while self._queue and (
            self._queue[0].match_id in self.bet_targets
            or self._status.get(self._queue[0].match_id) in self._stale_statuses
        ):
            self._queue.popleft()
        return self._queue[0] if self._queue else None

    def on_started(self, match_id: int) -> None:
        self._status[match_id] = "started"

    def on_half_time(self, match_id: int) -> None:
        self._status[match_id] = "half_time"

    def on_finished(self, match_id: int) -> None:
        self._status[match_id] = "finished"

    def arm(self) -> MatchDiscovered | None:
        """Called when the pattern fires. Returns the match to bet on now
        if the oldest still-viable queued announcement is not yet
        targeted and not yet stale per this instance's stale_statuses
        (i.e. still upcoming, or already started but not yet in one of
        those statuses) — pregame and early-live are both fair game,
        since BetExecutor prices the bet fresh either way and the
        stale-fire guard in place()/is_stale() still catches anything
        that reaches a stale status. Deliberately the OLDEST viable
        entry, not the newest one seen — a later fire (e.g. Patterns 4/5
        firing live, mid-round) can easily run after the real next round
        has already gone live and a further-out round has already been
        freshly announced behind it; the real next round must still win.
        Otherwise marks that a bet is owed to whichever match is
        discovered next."""
        candidate = self._next_viable_candidate()
        if candidate is not None:
            self.bet_targets.add(candidate.match_id)
            return candidate
        self._pending = True
        return None

    def debug_state(self) -> str:
        """Diagnostic snapshot for logging when arm() can't find an
        immediate target -- read-only, not used by any decision logic.
        Temporary instrumentation for tracking down why fired patterns
        sometimes sit pending for a long time (2026-09-16)."""
        if not self._queue:
            return "no match ever discovered yet"
        mid = self._queue[0].match_id
        return (
            f"oldest_queued=match {mid} status={self._status.get(mid)!r} "
            f"already_targeted={mid in self.bet_targets} pending={self._pending} "
            f"queue_depth={len(self._queue)} bet_targets_count={len(self.bet_targets)}"
        )

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
