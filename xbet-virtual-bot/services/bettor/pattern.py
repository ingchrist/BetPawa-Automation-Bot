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
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PatternTracker:
    low_threshold: int = 6
    streak_length: int = 3

    _streak: int = field(default=0, init=False, repr=False)
    _skip_next: bool = field(default=False, init=False, repr=False)
    _current_totals: list[int] = field(default_factory=list, init=False, repr=False)
    last_streak_totals: list[int] = field(default_factory=list, init=False)

    def process(self, first_half_total: int | None) -> bool:
        """Feed one more finished round's 1st-half total (home+away goals),
        in finish order. Returns True the moment this round is the
        streak_length-th consecutive qualifying round — the caller should
        bet on the *next* round when this returns True."""
        if self._skip_next:
            self._skip_next = False
            return False

        if first_half_total is None or first_half_total > self.low_threshold:
            self._streak = 0
            self._current_totals = []
            return False

        self._streak += 1
        self._current_totals.append(first_half_total)
        if self._streak >= self.streak_length:
            self.last_streak_totals = self._current_totals
            self._streak = 0
            self._current_totals = []
            self._skip_next = True
            return True
        return False
