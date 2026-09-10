"""PatternTracker — streak-detector state machine shared by both betting
patterns: Pattern 1 ("1st Half Over 6.5", direction="at_or_under") and
Pattern 2 ("2nd Half Under 7.5", direction="at_or_over"). Pure state
machine, no I/O: fed one finished round's combined goal total (1st-half
for Pattern 1, 2nd-half for Pattern 2) at a time, in the order rounds
actually finish. See docs/superpowers/specs/2026-09-09-first-half-over-
pattern-bettor-design.md and docs/superpowers/specs/2026-09-10-second-
half-under-pattern-bettor-design.md for each pattern's full rationale;
this module only encodes the shared mechanics.

Life cycle: `streak_length` consecutive qualifying rounds fire the pattern
(bet on the *next* round). "Qualifying" depends on `direction`:
`at_or_under` qualifies when total <= threshold (Pattern 1's shape);
`at_or_over` qualifies when total >= threshold (Pattern 2's shape). A
`None` total never qualifies, regardless of direction. After a fire, the
streak resets to 0 and the very next round processed is excluded from
counting entirely -- it's the round that was just bet on -- then the
round after that restarts the count from 0. Any non-qualifying round also
resets the streak to 0, but does *not* trigger a skip.

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
PatternDirection = Literal["at_or_under", "at_or_over"]


@dataclass
class PatternTracker:
    threshold: int = 6
    streak_length: int = 3
    direction: PatternDirection = "at_or_under"

    _streak: int = field(default=0, init=False, repr=False)
    _skip_next: bool = field(default=False, init=False, repr=False)
    _current_totals: list[int] = field(default_factory=list, init=False, repr=False)
    last_streak_totals: list[int] = field(default_factory=list, init=False)
    last_total: int | None = field(default=None, init=False)
    last_outcome: PatternOutcome | None = field(default=None, init=False)

    @property
    def streak(self) -> int:
        """Current in-progress streak count. Reads as 0 right after a
        reset, a skip, or a fire (the fire's own streak is in
        `last_streak_totals`, not here)."""
        return self._streak

    def _qualifies(self, total: int) -> bool:
        if self.direction == "at_or_under":
            return total <= self.threshold
        return total >= self.threshold

    def process(self, total: int | None) -> bool:
        """Feed one more finished round's combined goal total, in finish
        order. Returns True the moment this round is the
        streak_length-th consecutive qualifying round — the caller should
        bet on the *next* round when this returns True."""
        self.last_total = total

        if self._skip_next:
            self._skip_next = False
            self.last_outcome = "skipped"
            return False

        if total is None or not self._qualifies(total):
            self._streak = 0
            self._current_totals = []
            self.last_outcome = "reset"
            return False

        self._streak += 1
        self._current_totals.append(total)
        if self._streak >= self.streak_length:
            self.last_streak_totals = self._current_totals
            self._streak = 0
            self._current_totals = []
            self._skip_next = True
            self.last_outcome = "armed"
            return True
        self.last_outcome = "qualifying"
        return False
