"""PatternTracker — streak-detector state machine shared by three of this
codebase's four betting patterns: Pattern 1 ("1st Half Over 6.5",
direction="at_or_under"), Pattern 2 ("2nd Half Under 7.5",
direction="at_or_over"), and Pattern 3 ("1st Half Winner 2X",
direction="equals"). Pure state machine, no I/O: fed one finished round's
value (a combined goal total for Pattern 1/2, a "1X"/"2X"/"X" Double
Chance result for Pattern 3) at a time, in the order rounds actually
finish. See docs/superpowers/specs/2026-09-09-first-half-over-pattern-
bettor-design.md, docs/superpowers/specs/2026-09-10-second-half-under-
pattern-bettor-design.md, and docs/superpowers/specs/2026-09-13-first-
half-winner-2x-streak-pattern-bettor-design.md for each pattern's full
rationale; this module only encodes the shared mechanics.

This same module also holds `RoundPairStreakTracker`, below, for Pattern 4
("Main Game Under 16.5" pair streak) -- a different streak shape (it
groups rounds into pairs and counts qualifying values across both halves
of each pair, rather than a single scalar per round) that doesn't fit
`PatternTracker`'s per-round model, so it's a separate class rather than
a fourth `direction`. See docs/superpowers/plans/2026-09-16-main-game-
under-16.5-pair-streak-pattern.md and that class's own docstring for its
exact mechanics.

Life cycle: `streak_length` consecutive qualifying rounds fire the pattern
(bet on the *next* round). "Qualifying" depends on `direction`:
`at_or_under` qualifies when total <= threshold (Pattern 1's shape);
`at_or_over` qualifies when total >= threshold (Pattern 2's shape);
`equals` qualifies when total == threshold, a categorical match rather
than a numeric comparison (Pattern 3's shape, e.g. threshold="2X"). A
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
PatternDirection = Literal["at_or_under", "at_or_over", "equals"]


@dataclass
class PatternTracker:
    threshold: int | str = 6
    streak_length: int = 3
    direction: PatternDirection = "at_or_under"

    _streak: int = field(default=0, init=False, repr=False)
    _skip_next: bool = field(default=False, init=False, repr=False)
    _current_totals: list[int | str] = field(default_factory=list, init=False, repr=False)
    last_streak_totals: list[int | str] = field(default_factory=list, init=False)
    last_total: int | str | None = field(default=None, init=False)
    last_outcome: PatternOutcome | None = field(default=None, init=False)

    @property
    def streak(self) -> int:
        """Current in-progress streak count. Reads as 0 right after a
        reset, a skip, or a fire (the fire's own streak is in
        `last_streak_totals`, not here)."""
        return self._streak

    def _qualifies(self, total: int | str) -> bool:
        if self.direction == "equals":
            return total == self.threshold
        if self.direction == "at_or_under":
            return total <= self.threshold
        return total >= self.threshold

    def process(self, total: int | str | None) -> bool:
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


@dataclass
class RoundPairStreakTracker:
    """Pattern 4's counter — fires when >= required_count of the 4
    (1st-half-total, 2nd-half-total) values across 2 consecutive finished
    rounds are >= half_threshold. See docs/superpowers/specs/2026-09-16-
    main-game-under-16.5-pair-streak-pattern-design.md for the full
    rationale; this class only encodes the mechanics.

    Doesn't fit PatternTracker's shape as a parameterization:
    PatternTracker.process() takes one scalar and asks "does it
    individually qualify," accumulating a streak of *consecutive*
    qualifying rounds. This tracker instead buffers exactly 2 rounds'
    worth of *raw values* and evaluates a *count threshold* across all 4
    at once -- a genuinely different shape, not a different direction.

    Non-overlapping pairs: after 2 rounds are evaluated (fire or not),
    the pair buffer clears and a fresh pair starts counting from the
    very next round. On fire, the round immediately following is
    skipped entirely (not added to the next pair) before counting
    resumes -- same fire -> skip-one-round -> restart life cycle
    PatternTracker already has. A round with either half's total unknown
    (None) resets the pair buffer without counting, the same
    conservative stance PatternTracker takes for a None total.

    `last_qualifying_count`/`last_outcome`/`rounds_in_pair` are read-only
    reporting of what the most recent process() call did -- they don't
    change process()'s behavior or its bool return contract. On a
    "skipped" outcome, last_qualifying_count is left at its previous
    value (the just-completed pair's count) rather than recomputed --
    there's no meaningful qualifying-count for a single skipped round in
    this pair-based rule.
    """

    half_threshold: int = 9
    required_count: int = 3

    _pending: list[tuple[int, int]] = field(default_factory=list, init=False, repr=False)
    _skip_next: bool = field(default=False, init=False, repr=False)
    last_pair_values: list[int] = field(default_factory=list, init=False)
    last_qualifying_count: int = field(default=0, init=False)
    last_outcome: Literal["counting", "reset", "skipped", "armed"] | None = field(default=None, init=False)

    @property
    def rounds_in_pair(self) -> int:
        """0 or 1 -- how many rounds of the current pair have been
        counted so far. Reads as 0 right after a reset, a skip, or a
        fire (the fire's own pair is in last_pair_values, not here)."""
        return len(self._pending)

    def process(self, first_half_total: int | None, second_half_total: int | None) -> bool:
        """Feed one more finished round's two half-totals, in finish
        order. Returns True the moment this round completes a pair that
        meets required_count -- the caller should bet on the *next*
        round when this returns True."""
        if self._skip_next:
            self._skip_next = False
            self.last_outcome = "skipped"
            return False

        if first_half_total is None or second_half_total is None:
            self._pending = []
            self.last_qualifying_count = 0
            self.last_outcome = "reset"
            return False

        self._pending.append((first_half_total, second_half_total))
        if len(self._pending) < 2:
            self.last_qualifying_count = sum(
                1 for pair in self._pending for v in pair if v >= self.half_threshold
            )
            self.last_outcome = "counting"
            return False

        values = [v for pair in self._pending for v in pair]
        qualifying = sum(1 for v in values if v >= self.half_threshold)
        self.last_pair_values = values
        self.last_qualifying_count = qualifying
        self._pending = []
        if qualifying >= self.required_count:
            self._skip_next = True
            self.last_outcome = "armed"
            return True
        self.last_outcome = "reset"
        return False
