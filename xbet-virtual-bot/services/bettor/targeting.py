"""TargetTracker — resolves which upcoming match a fired pattern should
bet on, and guards against betting into a match that's already past this
instance's configured stale statuses by the time the bet is actually
attempted.

Pure, no I/O — fed only the MatchDiscovered/MatchStarted/MatchHalfTime/
MatchFinished events services/bettor/main.py already subscribes to. The
"single announced next match" semantics come from the aggregator (see
_update_upcoming_queue in services/aggregator/state.py) — but that
announcement is re-issued (superseding the previous one) the instant the
identity of "the soonest upcoming match" changes, which is NOT always in
discovery-arrival order: this league keeps several matches "upcoming" at
once, each with its own countdown, and kickoff estimates for them
genuinely move (observed 10s-6min variance per state.py's own comment) —
so a later MatchDiscovered can correct an earlier, still-unstarted one
that turns out not to be the real next round after all. (Live incident,
2026-09-20 23:00:59: Braga vs Anderlecht was announced first, then
West Ham vs Lille OSC was announced as the actual sooner match -- but
West Ham hadn't started yet either, so a naive oldest-first FIFO still
picked the stale Braga announcement over it.) This module therefore
keeps every still-viable announcement it's seen and, rather than trusting
raw arrival order, ranks them the same way the aggregator itself decides
"soonest": an already-kicked-off match always outranks a still-upcoming
one (that's a fact, not an estimate), and among still-upcoming matches
the one with the earliest known `kickoff_ts` wins. `arm()` resolves to
that top-ranked still-viable entry: a pattern that fires live, mid-round
(Patterns 4/5), or any time after `place()` has some async delay, can
easily run after the real next round has already gone live *and* a
further-out round has already been freshly announced behind it — the
real next round must still win, since "started" alone doesn't make a
tracker's own configured stale_statuses consider it too late to bet.
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
        self._entries: dict[int, MatchDiscovered] = {}
        self._order: dict[int, int] = {}  # match_id -> discovery sequence, tie-break only
        self._next_order = 0
        self._status: dict[int, str] = {}
        self._pending = False
        self._stale_statuses = set(stale_statuses) if stale_statuses is not None else set(_DEFAULT_STALE_STATUSES)
        self.bet_targets: set[int] = set()

    def on_discovered(self, event: MatchDiscovered) -> MatchDiscovered | None:
        """Record this announcement. If a fire is waiting for a target
        (pending), resolve it now to the best still-viable known
        announcement and return it for immediate betting."""
        self._status[event.match_id] = "upcoming"
        if event.match_id not in self._entries:
            self._order[event.match_id] = self._next_order
            self._next_order += 1
        self._entries[event.match_id] = event
        if self._pending:
            candidate = self._next_viable_candidate()
            if candidate is not None:
                self._pending = False
                self.bet_targets.add(candidate.match_id)
                return candidate
        return None

    def _priority(self, match_id: int) -> tuple[int, int, int]:
        """Lower sorts first. Rank 0 (already kicked off) always beats
        rank 1 (still upcoming) -- a match that's actually live is a
        fact, never a stale estimate. Within a rank, earlier kickoff_ts
        wins (mirrors the aggregator's own "soonest" comparison in
        _update_upcoming_queue), then discovery order as a final,
        deterministic tie-break."""
        status_rank = 0 if self._status.get(match_id) != "upcoming" else 1
        return (status_rank, self._entries[match_id].kickoff_ts, self._order[match_id])

    def _next_viable_candidate(self) -> MatchDiscovered | None:
        """Returns the highest-priority still-viable known announcement
        (not already bet on, not past this instance's stale_statuses —
        genuinely too late, not merely started), without consuming it.
        See _priority for the ranking; this is no longer a strict
        discovery-order queue, since discovery order isn't reliably
        kickoff order (see module docstring)."""
        viable_ids = [
            match_id
            for match_id in self._entries
            if match_id not in self.bet_targets and self._status.get(match_id) not in self._stale_statuses
        ]
        if not viable_ids:
            return None
        best_id = min(viable_ids, key=self._priority)
        return self._entries[best_id]

    def on_started(self, match_id: int) -> None:
        self._status[match_id] = "started"

    def on_half_time(self, match_id: int) -> None:
        self._status[match_id] = "half_time"

    def on_finished(self, match_id: int) -> None:
        self._status[match_id] = "finished"

    def arm(self) -> MatchDiscovered | None:
        """Called when the pattern fires. Returns the match to bet on now
        if the best still-viable known announcement (see _priority) is
        not yet targeted and not yet stale per this instance's
        stale_statuses (i.e. still upcoming, or already started but not
        yet in one of those statuses) — pregame and early-live are both
        fair game, since BetExecutor prices the bet fresh either way and
        the stale-fire guard in place()/is_stale() still catches anything
        that reaches a stale status. Deliberately the real next round,
        not just the newest announcement seen — a later fire (e.g.
        Patterns 4/5 firing live, mid-round) can easily run after the
        real next round has already gone live *and* a further-out round
        has already been freshly announced behind it; the real next round
        must still win. Otherwise marks that a bet is owed to whichever
        match is discovered next."""
        candidate = self._next_viable_candidate()
        if candidate is not None:
            self.bet_targets.add(candidate.match_id)
            return candidate
        self._pending = True
        return None

    def debug_state(self) -> str:
        """Diagnostic snapshot for logging when arm() can't find an
        immediate target -- read-only, not used by any decision logic
        (doesn't touch bet_targets or _pending). Temporary
        instrumentation for tracking down why fired patterns sometimes
        sit pending for a long time (2026-09-16)."""
        if not self._entries:
            return "no match ever discovered yet"
        candidate = self._next_viable_candidate()
        if candidate is None:
            return (
                f"no viable candidate among {len(self._entries)} known match(es) "
                f"(all already targeted or stale) pending={self._pending}"
            )
        mid = candidate.match_id
        return (
            f"best_candidate=match {mid} status={self._status.get(mid)!r} "
            f"already_targeted={mid in self.bet_targets} pending={self._pending} "
            f"known_count={len(self._entries)} bet_targets_count={len(self.bet_targets)}"
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
