"""TargetTracker — resolves which upcoming match a fired pattern should
bet on, and guards against betting into a match whose 1st half is already
over by the time the bet is actually attempted.

Pure, no I/O — fed only the MatchDiscovered/MatchStarted/MatchHalfTime/
MatchFinished events services/bettor/main.py already subscribes to. The
"single announced next match" semantics this relies on are the
aggregator's own (see _update_upcoming_queue in services/aggregator/
state.py) — this module just tracks the latest one it's seen.
"""
from __future__ import annotations

from shared.events import MatchDiscovered

_STALE_STATUSES = {"half_time", "finished"}


class TargetTracker:
    def __init__(self) -> None:
        self._latest_discovered: MatchDiscovered | None = None
        self._status: dict[int, str] = {}
        self._pending = False
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
        if it's already known, not yet targeted, and not yet stale (i.e.
        still upcoming, or already started but not yet at half-time) —
        pregame and early-live are both fair game, since BetExecutor
        prices the bet fresh either way and the stale-fire guard in
        place()/is_stale() still catches anything past half-time.
        Otherwise marks that a bet is owed to whichever match is
        discovered next."""
        latest = self._latest_discovered
        if (
            latest is not None
            and latest.match_id not in self.bet_targets
            and self._status.get(latest.match_id) not in _STALE_STATUSES
        ):
            self.bet_targets.add(latest.match_id)
            return latest
        self._pending = True
        return None

    def is_stale(self, match_id: int) -> bool:
        """True if this match's 1st half is already known to be over —
        checked immediately before actually clicking a bet, since real
        wall-clock time passes during browser navigation while this
        tracker keeps receiving events in the background."""
        return self._status.get(match_id) in _STALE_STATUSES
