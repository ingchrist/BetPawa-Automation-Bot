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
a fourth `direction`. Like `SecondHalfLiveConfirmTracker` below, it isn't
fed one already-known value per round either -- each of the pair's up to
4 half-totals is fed the moment it's known (1st half at half-time, 2nd
half live during the 2nd half or as a safety net at full time), and it
fires the instant the running qualifying count hits `required_count`,
which can happen mid-round. See docs/superpowers/plans/2026-09-16-main-
game-under-16.5-pair-streak-pattern.md and that class's own docstring for
its exact mechanics.

Also holds `SecondHalfLiveConfirmTracker`, below, for Pattern 5 ("Main
Game Under" on a single round whose 1st-half AND 2nd-half totals are
BOTH >= a threshold) -- unlike every tracker above, it isn't fed one
already-known value per round; it's fed a live, still-changing goal
stream during the 2nd half and decides mid-round, the moment both halves
have independently reached the threshold, instead of waiting for the
round to finish. See that class's own docstring for the exact mechanics.

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
    (1st-half-total, 2nd-half-total) values across 2 consecutive rounds
    are >= half_threshold. See docs/superpowers/specs/2026-09-16-
    main-game-under-16.5-pair-streak-pattern-design.md for the original
    rationale; this class encodes the mechanics, now live.

    Doesn't fit PatternTracker's shape as a parameterization:
    PatternTracker.process() takes one scalar and asks "does it
    individually qualify," accumulating a streak of *consecutive*
    qualifying rounds. This tracker instead buffers up to 2 rounds'
    worth of *raw values* and evaluates a *count threshold* across all 4
    at once -- a genuinely different shape, not a different direction.

    Unlike a batch-fed tracker, this one isn't handed a round's two
    half-totals together once the round is done. Each of the pair's up
    to 4 slots is fed the moment it's individually known -- a round's
    1st half at MatchHalfTime, its 2nd half either live during the 2nd
    half (the moment the running total first reaches half_threshold, via
    on_score_changed) or, if that never happens, as a safety net at
    MatchFinished (on_finished) -- and the qualifying count is
    rechecked after every single slot. The pattern fires the instant
    that count reaches required_count, which can happen before all 4
    slots are known: e.g. a pair whose first round already qualified on
    both halves fires the moment the second round's 1st half alone also
    qualifies, without waiting for that round's 2nd half to even start;
    or, if only 2 of the first 3 known values qualify, fires mid-2nd-half
    the instant the live-running total crosses half_threshold, without
    waiting for MatchFinished. `on_score_changed`'s live-running total
    is derived the same way SecondHalfLiveConfirmTracker's is: cumulative
    goals at this moment minus the 1st-half baseline recorded at
    MatchHalfTime (MatchScoreChanged carries cumulative goals, not a
    half-scoped count).

    Non-overlapping pairs: after a pair's 4 slots are known (fire or
    not), the pair buffer clears and a fresh pair starts counting from
    the very next round's 1st half. On fire, the round immediately
    following the one that supplied the fire's last qualifying slot is
    skipped entirely (not added to the next pair) before counting
    resumes -- same fire -> skip-one-round -> restart life cycle
    PatternTracker already has, unchanged by fires now being able to
    happen mid-round. A round whose 1st- or 2nd-half total is unknown
    (None) resets the whole pending pair, the same conservative stance
    PatternTracker takes for a None total.

    Early dead-pair reset: the pair also resets the moment required_count
    becomes mathematically unreachable, without waiting for all 4 slots to
    be known -- e.g. a round whose own two halves both come in under
    half_threshold (0 of its 2 slots qualify) already leaves at most 2 of
    the remaining 2 slots reachable, one short of required_count=3, so the
    pair resets right there instead of dragging that dead round into a
    wasted round 2. Checked after every new slot (`_is_pair_dead`, off
    `required_count - last_qualifying_count > remaining slots out of 4`);
    the round supplying the slot that kills the pair is discarded the same
    way a None total discards one -- the next fresh pair starts counting
    from the round after it, not from it.

    `last_qualifying_count`/`last_outcome`/`rounds_in_pair` are read-only
    reporting of what the most recent on_half_time/on_score_changed/
    on_finished call did -- they don't change that call's return
    contract. On a "skipped" outcome, last_qualifying_count is left at
    its previous value (the just-fired pair's count) rather than
    recomputed -- there's no meaningful qualifying-count for a single
    skipped round in this pair-based rule.

    `on_half_time`/`on_score_changed`/`on_finished` each return `True`
    the moment a pair is confirmed to fire (bet the *next* round),
    `False` if this call conclusively resolved something without firing
    (a None-total reset, a skip consumed, a pair completing its 4th
    slot without reaching required_count, or the pair going
    mathematically dead earlier than that), and `None` if this call
    didn't change anything reportable -- a 2nd-half score change that
    hasn't yet reached half_threshold, one for a round whose own
    MatchHalfTime hasn't arrived yet, a 1st-half score change (never
    watched), or a round already resolved by an earlier call.
    """

    half_threshold: int = 9
    required_count: int = 3

    _pair_match_ids: list[int] = field(default_factory=list, init=False, repr=False)
    _pair_h1: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _pair_h2: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _live_baseline: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _resolved: set[int] = field(default_factory=set, init=False, repr=False)
    _skip_next: bool = field(default=False, init=False, repr=False)

    last_pair_values: list[int] = field(default_factory=list, init=False)
    last_qualifying_count: int = field(default=0, init=False)
    last_outcome: Literal["counting", "reset", "skipped", "armed"] | None = field(default=None, init=False)

    @property
    def rounds_in_pair(self) -> int:
        """0 or 1 -- how many rounds of the current pair have at least
        their 1st half recorded so far. Reads as 0 right after a reset,
        a skip, or a fire (the fire's own pair is in last_pair_values,
        not here)."""
        return len(self._pair_match_ids)

    def _pair_values(self) -> list[int]:
        values: list[int] = []
        for match_id in self._pair_match_ids:
            if match_id in self._pair_h1:
                values.append(self._pair_h1[match_id])
            if match_id in self._pair_h2:
                values.append(self._pair_h2[match_id])
        return values

    def _reset_pair(self) -> None:
        self._pair_match_ids = []
        self._pair_h1 = {}
        self._pair_h2 = {}

    def _check_and_fire(self) -> bool:
        values = self._pair_values()
        qualifying = sum(1 for v in values if v >= self.half_threshold)
        self.last_pair_values = values
        self.last_qualifying_count = qualifying
        if qualifying >= self.required_count:
            self._reset_pair()
            self._skip_next = True
            self.last_outcome = "armed"
            return True
        return False

    def _is_pair_dead(self) -> bool:
        """True once the known values already make required_count
        unreachable -- i.e. even if every still-unknown slot in this
        pair (out of 4 total) went on to qualify, the qualifying count
        couldn't reach required_count. Must be called right after
        _check_and_fire() (which populates last_qualifying_count off the
        current _pair_values()) and before any reset of the pair."""
        remaining_slots = 4 - len(self._pair_values())
        return self.last_qualifying_count + remaining_slots < self.required_count

    def _record_h2(self, match_id: int, value: int) -> bool:
        self._pair_h2[match_id] = value
        self._live_baseline.pop(match_id, None)
        self._resolved.add(match_id)

        if self._check_and_fire():
            return True
        if self._is_pair_dead():
            self._reset_pair()
            self.last_outcome = "reset"
        else:
            self.last_outcome = "counting"
        return False

    def on_half_time(self, match_id: int, first_half_total: int | None) -> bool | None:
        """Feed a round's 1st-half total the moment it's known. Records
        it as the pair's next slot and checks immediately whether that
        alone is enough to fire -- returns True without waiting for this
        round's own 2nd half if so."""
        if self._skip_next:
            self._skip_next = False
            self._resolved.add(match_id)
            self.last_outcome = "skipped"
            return False

        if match_id in self._resolved:
            return None

        if first_half_total is None:
            self._reset_pair()
            self._resolved.add(match_id)
            self.last_qualifying_count = 0
            self.last_outcome = "reset"
            return False

        self._pair_match_ids.append(match_id)
        self._pair_h1[match_id] = first_half_total

        if self._check_and_fire():
            self._resolved.add(match_id)
            return True

        if self._is_pair_dead():
            self._reset_pair()
            self._resolved.add(match_id)
            self.last_outcome = "reset"
            return False

        self._live_baseline[match_id] = first_half_total
        self.last_outcome = "counting"
        return False

    def on_score_changed(
        self, match_id: int, period_label: str, home_goals: int, away_goals: int
    ) -> bool | None:
        """Feed a live goal update. Only acts while this round's 1st
        half is already recorded and it's currently in its 2nd half --
        the moment the derived 2nd-half-so-far count first reaches
        half_threshold, that slot resolves right there, mid-round."""
        if match_id in self._resolved or period_label != "2nd half":
            return None
        baseline = self._live_baseline.get(match_id)
        if baseline is None:
            return None
        running = (home_goals + away_goals) - baseline
        if running < self.half_threshold:
            return None
        return self._record_h2(match_id, running)

    def on_finished(
        self, match_id: int, first_half_total: int | None, second_half_total: int | None
    ) -> bool | None:
        """Safety net: resolves this round's 1st half (if MatchHalfTime
        was somehow never seen for it) and/or 2nd half (if no live score
        change ever crossed half_threshold) using the final settled
        totals. A no-op for a round already resolved -- fired, skipped,
        reset, or resolved live."""
        if match_id in self._resolved:
            return None

        if match_id not in self._pair_h1:
            if first_half_total is None:
                self._reset_pair()
                self._resolved.add(match_id)
                self.last_qualifying_count = 0
                self.last_outcome = "reset"
                return False
            self._pair_match_ids.append(match_id)
            self._pair_h1[match_id] = first_half_total

        if second_half_total is None:
            self._reset_pair()
            self._live_baseline.pop(match_id, None)
            self._resolved.add(match_id)
            self.last_qualifying_count = 0
            self.last_outcome = "reset"
            return False

        return self._record_h2(match_id, second_half_total)


@dataclass
class SecondHalfLiveConfirmTracker:
    """Pattern 5's counter -- fires when a single round's 1st-half total
    AND 2nd-half total are BOTH >= threshold, checked live during the 2nd
    half so a qualifying round can fire mid-round instead of waiting for
    MatchFinished.

    This is an AND across the two halves of *one* round -- not
    RoundPairStreakTracker's shape (a count across two consecutive
    rounds). The 1st half's total is only knowable once, at half-time, so
    it's only ever checked there: if it's already < threshold at that
    point the round is dead -- it can never qualify no matter what the
    2nd half does -- and this stops watching it. Only when the 1st half
    is already >= threshold does the 2nd half's live, still-climbing goal
    count get watched at all. shared.events.MatchScoreChanged carries the
    match's running *cumulative* goals plus which half is in progress,
    not a half-scoped count, so the 2nd-half-so-far count is derived as
    (cumulative goals at this moment) - (the 1st-half baseline recorded
    off MatchHalfTime). The instant that derived count first reaches
    threshold, the round fires right there -- it does not wait for that
    2nd half, or the round, to actually finish playing out.

    Reuses PatternTracker internally for the fire -> skip-one-round ->
    restart life cycle (streak_length is fixed at 1: this pattern's own
    definition is "1 round" qualifying, not a run of several) -- what
    differs here is only *when* a round's qualifying decision is made;
    it's fed into that inner tracker exactly once per round, in round
    order, the same contract PatternTracker itself already requires.

    `on_half_time`/`on_score_changed`/`on_finished` each return `True` the
    moment this round is confirmed to fire (bet the *next* round), `False`
    if this round was just conclusively resolved without firing (dead at
    half-time, a reset at full time, or a skip consumed), and `None` if
    this call didn't change anything reportable -- still pending on the
    2nd half, a 2nd-half score change for a round whose own MatchHalfTime
    hasn't arrived yet (the aggregator can emit those in that order for
    the same snapshot -- see MatchStateMachine.process()), a 1st-half
    score change (never watched -- only MatchHalfTime settles the 1st
    half), or a round already resolved by an earlier call.
    """

    threshold: int = 9

    _inner: PatternTracker = field(init=False, repr=False)
    _pending_baseline: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _resolved: set[int] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        self._inner = PatternTracker(threshold=self.threshold, streak_length=1, direction="at_or_over")

    @property
    def streak(self) -> int:
        return self._inner.streak

    @property
    def last_total(self) -> int | str | None:
        return self._inner.last_total

    @property
    def last_outcome(self) -> PatternOutcome | None:
        return self._inner.last_outcome

    def _resolve(self, match_id: int, total: int | None) -> bool:
        self._resolved.add(match_id)
        self._pending_baseline.pop(match_id, None)
        return self._inner.process(total)

    def on_half_time(self, match_id: int, first_half_total: int | None) -> bool | None:
        """Feed a round's 1st-half total the moment it's known. Resolves
        the round immediately (dead) if it's below threshold; otherwise
        leaves it pending on the 2nd half."""
        if match_id in self._resolved:
            return None
        if first_half_total is None or first_half_total < self.threshold:
            return self._resolve(match_id, first_half_total)
        self._pending_baseline[match_id] = first_half_total
        return None

    def on_score_changed(
        self, match_id: int, period_label: str, home_goals: int, away_goals: int
    ) -> bool | None:
        """Feed a live goal update. Only acts while this round is pending
        (1st half already >= threshold) and currently in its 2nd half."""
        if match_id in self._resolved or period_label != "2nd half":
            return None
        baseline = self._pending_baseline.get(match_id)
        if baseline is None:
            return None
        running = (home_goals + away_goals) - baseline
        if running < self.threshold:
            return None
        return self._resolve(match_id, running)

    def on_finished(
        self, match_id: int, first_half_total: int | None, second_half_total: int | None
    ) -> bool | None:
        """Safety net: resolves any round the live path never got to --
        e.g. a missed MatchScoreChanged -- using the final settled
        totals. A no-op for a round already resolved live."""
        if match_id in self._resolved:
            return None
        value = first_half_total if (first_half_total is None or first_half_total < self.threshold) else second_half_total
        return self._resolve(match_id, value)
