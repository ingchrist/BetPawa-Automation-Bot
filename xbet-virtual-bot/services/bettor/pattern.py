"""PatternTracker — Pattern 1: "Total 1st Half Over 6.5" streak detector.

Pure state machine, no I/O: fed one finished round's 1st-half combined
goal total at a time, in the order rounds actually finish. See the design
doc for the pattern's full rationale (docs/superpowers/specs/
2026-09-09-first-half-over-pattern-bettor-design.md); this module only
encodes its mechanics.

Life cycle: 3 consecutive rounds with total <= low_threshold fire the
pattern (bet on the *next* round). After a fire, the streak resets to 0
and the very next round processed is excluded from counting entirely —
it's the round that was just bet on — then the round after that restarts
the count from 0. Any round above the threshold (or with an unknown
total) also resets the streak to 0, but does *not* trigger a skip.

`streak`/`last_total`/`last_outcome` are read-only reporting of what the
most recent `process()` call did — for a caller (e.g. the bettor's
main.py) that wants to narrate every round's effect on the streak, not
just the moment it fires. They don't change `process()`'s behavior or
its bool return contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PatternOutcome = Literal["qualifying", "reset", "skipped", "armed"]


@dataclass
class PatternTracker:
    low_threshold: int = 6
    streak_length: int = 3

    _streak: int = field(default=0, init=False, repr=False)
    _skip_next: bool = field(default=False, init=False, repr=False)
    _current_totals: list[int] = field(default_factory=list, init=False, repr=False)
    last_streak_totals: list[int] = field(default_factory=list, init=False)
    last_total: int | None = field(default=None, init=False)
    last_outcome: PatternOutcome | None = field(default=None, init=False)

    @property
    def streak(self) -> int:
        """Current in-progress streak count. Reads as 0 right after a
        reset, a skip, or a fire (the fire's own 3-round streak is in
        `last_streak_totals`, not here)."""
        return self._streak

    def process(self, first_half_total: int | None) -> bool:
        """Feed one more finished round's 1st-half total (home+away goals),
        in finish order. Returns True the moment this round is the
        streak_length-th consecutive qualifying round — the caller should
        bet on the *next* round when this returns True."""
        self.last_total = first_half_total

        if self._skip_next:
            self._skip_next = False
            self.last_outcome = "skipped"
            return False

        if first_half_total is None or first_half_total > self.low_threshold:
            self._streak = 0
            self._current_totals = []
            self.last_outcome = "reset"
            return False

        self._streak += 1
        self._current_totals.append(first_half_total)
        if self._streak >= self.streak_length:
            self.last_streak_totals = self._current_totals
            self._streak = 0
            self._current_totals = []
            self._skip_next = True
            self.last_outcome = "armed"
            return True
        self.last_outcome = "qualifying"
        return False
