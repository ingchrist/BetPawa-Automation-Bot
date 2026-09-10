# Pattern 2 ("2nd Half Under 7.5" streak) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, independent automated betting pattern to the already-live `services/bettor/` process — detect 3 consecutive finished rounds with a 2nd-half combined goal total ≥ 8, then place a real bet on the next round's `Total. 2nd half` market, `Under 7.5`, via the already-implemented `BetExecutor` API client.

**Architecture:** No new service. `PatternTracker`, `TargetTracker`, and `BetExecutor` — Pattern 1's existing pure building blocks — each gain one small, backward-compatible constructor/method parameter (`direction`, `stale_statuses`, `period`/`over`) rather than being forked. `services/bettor/main.py` runs a second, parallel instance of each, coordinated through a new pure `mutual_exclusion_reason()` helper so the two patterns never both stake money on the same match in the same round. `shared/events.py`'s bettor event models drop their Pattern-1-only hardcoded assumptions (`market_label`/`condition_label`/`period_total`/`pattern_name`/`direction` replace hardcoded "1st half" text), and `services/display/render.py` is updated to print those fields verbatim instead of hardcoding Pattern 1's wording — this makes `render.py` fully pattern-agnostic, so Pattern 2 needs no render.py changes of its own once that's done.

**Tech Stack:** Python 3.10, asyncio, pydantic, Redis pub/sub (existing `shared/bus.py`), `rich` (existing `services/display`), `httpx` (existing `services/bettor/betting_api.py`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-10-second-half-under-pattern-bettor-design.md`

## Global Constraints

- Pattern 2: 3 consecutive finished rounds with 2nd-half total ≥ `PATTERN2_HIGH_THRESHOLD` (default `8`) fires a bet on the next round's `Total. 2nd half` `Under PATTERN2_BET_LINE` (default `7.5`). Streak length is `PATTERN2_STREAK_LENGTH`, default `3`.
- After a fire, the streak resets to 0 and the very next finished round is excluded from counting (it's the one just bet on) — the round after that restarts the count from 0. Identical life-cycle to Pattern 1, just evaluated on 2nd-half totals in the opposite direction.
- Pattern 2 evaluates at `MatchFinished` (not `MatchHalfTime` like Pattern 1) — `MatchFinished.second_half` is the only event carrying a final 2nd-half score.
- Stake is `PATTERN2_BET_STAKE_AMOUNT`, default `90` (FCFA) — independently configurable from Pattern 1's `BET_STAKE_AMOUNT`, never hardcoded at the call site.
- Bets go live from the very first run — no dry-run gate, same as Pattern 1 (explicit prior user decision this plan does not revisit). The very first real Pattern 2 bet through `period=2, over=False` needs explicit user authorization before it happens — see Task 8.
- Mutual exclusion: Pattern 1 and Pattern 2 must never both place a bet on the same `match_id` in the same round. Whichever pattern's target resolves first wins the match; the other logs a `BetFailed` (reason starting `"mutual exclusion: ..."`) and does not call the executor, but its own tracker/target-tracker state still advances normally.
- `services/bettor/betting_api.py`'s `FIRST_HALF_ID_OFFSET = 1` sub-id fix (betting against `match_id + 1`/`match_id + 2`, never the raw ambiguous `match_id`) is an existing, already-tested, currently-uncommitted foundation this plan builds on as-is — this plan does not touch or re-verify it, and does not include a task to commit it separately (explicit prior user decision to commit it together with other pending work later).
- Every existing Pattern 1 test (`tests/test_pattern.py`, `tests/test_betting_api.py`, `tests/test_targeting.py`) must still pass, unmodified in behavior, after every task in this plan — Pattern 1 is live, real-money software; nothing here may change its behavior.

---

## Task 1: Generalize `PatternTracker` with a `direction` parameter

**Files:**
- Modify: `services/bettor/pattern.py`
- Modify: `tests/test_pattern.py`
- Modify: `services/bettor/main.py:55`

**Interfaces:**
- Consumes: nothing new.
- Produces: `PatternTracker(threshold: int = 6, streak_length: int = 3, direction: Literal["at_or_under", "at_or_over"] = "at_or_under")` — the `threshold`/`direction` names are new; `.process(total: int | None) -> bool`, `.streak`, `.last_total`, `.last_outcome`, `.last_streak_totals` are unchanged in meaning. Consumed by Task 6 (Pattern 2's own tracker instance) and already consumed by Task 5's updated `main.py`.

- [ ] **Step 1: Rename the three existing tests' `low_threshold=` kwarg to `threshold=`**

In `tests/test_pattern.py`, this is a pure rename — no behavior change. Update these three call sites:

```python
def test_boundary_value_at_threshold_qualifies():
    tracker = PatternTracker(threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_boundary_value_above_threshold_does_not_qualify():
    tracker = PatternTracker(threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(7) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_streak_length_and_threshold_are_configurable():
    tracker = PatternTracker(threshold=5, streak_length=2)
    assert tracker.process(5) is False
    assert tracker.process(5) is True
    assert tracker.last_streak_totals == [5, 5]
```

- [ ] **Step 2: Append new tests for `direction="at_or_over"`**

Add to the end of `tests/test_pattern.py`:

```python
def test_at_or_over_direction_fires_on_three_consecutive_high_rounds():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    assert tracker.process(9) is False
    assert tracker.process(10) is False
    assert tracker.process(8) is True
    assert tracker.last_streak_totals == [9, 10, 8]


def test_at_or_over_direction_low_round_breaks_the_streak():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    assert tracker.process(9) is False
    assert tracker.process(7) is False  # breaks it -- below threshold
    assert tracker.process(9) is False  # restarts at 1
    assert tracker.process(9) is False
    assert tracker.process(9) is True


def test_at_or_over_direction_none_total_breaks_the_streak():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    assert tracker.process(9) is False
    assert tracker.process(None) is False
    assert tracker.process(9) is False
    assert tracker.process(9) is False
    assert tracker.process(9) is True


def test_at_or_over_direction_fire_then_skip_one_round_then_restart():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    tracker.process(9)
    tracker.process(10)
    assert tracker.process(8) is True  # fires

    assert tracker.process(2) is False  # the skipped bet-target round

    assert tracker.process(9) is False
    assert tracker.process(9) is False
    assert tracker.process(9) is True
    assert tracker.last_streak_totals == [9, 9, 9]


def test_at_or_over_direction_progress_reporting():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")

    tracker.process(9)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (1, 9, "qualifying")

    tracker.process(3)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 3, "reset")

    tracker.process(9)
    tracker.process(10)
    tracker.process(8)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 8, "armed")

    tracker.process(2)  # the skipped bet-target round
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 2, "skipped")


def test_at_or_under_direction_default_is_pattern_1s_exact_existing_behavior():
    tracker = PatternTracker()  # direction defaults to "at_or_under"
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True
    assert tracker.last_streak_totals == [4, 5, 6]
```

- [ ] **Step 3: Run the tests to verify the new ones fail and confirm which existing ones break**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: the 3 renamed tests fail with `TypeError: __init__() got an unexpected keyword argument 'threshold'`; the 6 new `direction`-based tests fail the same way (`'direction'` instead); all other existing tests still pass unchanged.

- [ ] **Step 4: Implement the generalized `PatternTracker`**

Replace the full contents of `services/bettor/pattern.py`:

```python
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
```

- [ ] **Step 5: Update `services/bettor/main.py`'s Pattern 1 construction site**

In `services/bettor/main.py`, replace:

```python
    tracker = PatternTracker(low_threshold=config.pattern_low_threshold, streak_length=config.pattern_streak_length)
```

with:

```python
    tracker = PatternTracker(threshold=config.pattern_low_threshold, streak_length=config.pattern_streak_length)
```

(`config.pattern_low_threshold` — the `Config` field name — is untouched; only the constructor keyword changes.)

- [ ] **Step 6: Run the tests to verify they all pass**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: all tests pass (9 original + 6 new = 15).

Run: `.venv/bin/python -c "import services.bettor.main"`
Expected: no exception.

- [ ] **Step 7: Commit**

```bash
git add services/bettor/pattern.py tests/test_pattern.py services/bettor/main.py
git commit -m "$(cat <<'EOF'
Generalize PatternTracker with a direction parameter

Adds direction: "at_or_under" | "at_or_over" (default "at_or_under",
Pattern 1's exact existing behavior unchanged) so Pattern 2's opposite-
direction streak can reuse the same state machine instead of forking it.
Renames the low_threshold field to threshold (class-internal only -- the
PATTERN_LOW_THRESHOLD env var / Config field name is untouched).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016NyjMseHDRFt6oRPAAK1X1
EOF
)"
```

---

## Task 2: Extend `BetExecutor.place_bet()` with `period`/`over` parameters

**Files:**
- Modify: `services/bettor/betting_api.py`
- Modify: `tests/test_betting_api.py`

**Interfaces:**
- Consumes: `TOTAL_OVER_T`, `TOTAL_UNDER_T` from `services/collector/xbet_client.py` (both already defined, `T=9`/`T=10`).
- Produces: `BetExecutor.place_bet(match_id, home, away, stake, line=6.5, period: Literal[1, 2] = 1, over: bool = True) -> BetResult` — the two new parameters both default to Pattern 1's existing exact behavior. Consumed by Task 6 (Pattern 2 calls with `period=2, over=False`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_betting_api.py`, first updating the `_matching_event` helper to accept a `t` (market type) parameter:

```python
def _matching_event(coef: float = 1.408, line: float = 6.5, t: int = 9) -> dict:
    return {"T": t, "P": line, "G": 17, "C": coef}
```

Then append two new tests:

```python
def test_period_2_over_false_targets_second_half_under():
    seen_ids = []
    seen_types = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            seen_ids.append(int(request.url.params["id"]))
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=7.5)]))
        body = json.loads(request.content)
        seen_ids.append(body["Events"][0]["GameId"])
        seen_types.append(body["Events"][0]["Type"])
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=751444117, home="A", away="B", stake=90, line=7.5, period=2, over=False,
        )
    )
    asyncio.run(executor.aclose())

    assert seen_ids == [751444119, 751444119]  # match_id + SECOND_HALF_ID_OFFSET
    assert seen_types == [10]
    assert result.success is True


def test_period_and_over_defaults_leave_pattern_1s_request_shape_unchanged():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event()]))
        body = json.loads(request.content)
        assert body["Events"][0]["GameId"] == 751444118  # match_id + FIRST_HALF_ID_OFFSET
        assert body["Events"][0]["Type"] == 9
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(match_id=751444117, home="A", away="B", stake=90, line=6.5)
    )
    asyncio.run(executor.aclose())

    assert result.success is True
```

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/test_betting_api.py -v`
Expected: `test_period_2_over_false_targets_second_half_under` fails with a `TypeError: place_bet() got an unexpected keyword argument 'period'`; all other (pre-existing) tests still pass.

- [ ] **Step 3: Implement the extension**

In `services/bettor/betting_api.py`:

Replace the import line:

```python
from services.collector.xbet_client import TOTAL_OVER_T
```

with:

```python
from services.collector.xbet_client import TOTAL_OVER_T, TOTAL_UNDER_T
```

Replace:

```python
from dataclasses import dataclass
from typing import Awaitable, Callable
```

with:

```python
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
```

Replace:

```python
FIRST_HALF_ID_OFFSET = 1
TOTALS_GROUP = 17
```

with:

```python
FIRST_HALF_ID_OFFSET = 1
SECOND_HALF_ID_OFFSET = 2
TOTALS_GROUP = 17
```

Replace the `place_bet` signature and its first two lines:

```python
    async def place_bet(
        self, match_id: int, home: str, away: str, stake: float, line: float = 6.5
    ) -> BetResult:
        game_id = match_id + FIRST_HALF_ID_OFFSET
```

with:

```python
    async def place_bet(
        self,
        match_id: int,
        home: str,
        away: str,
        stake: float,
        line: float = 6.5,
        period: Literal[1, 2] = 1,
        over: bool = True,
    ) -> BetResult:
        offset = FIRST_HALF_ID_OFFSET if period == 1 else SECOND_HALF_ID_OFFSET
        game_id = match_id + offset
        bet_type = TOTAL_OVER_T if over else TOTAL_UNDER_T
```

Replace:

```python
        try:
            coef = await self._current_odds(game_id, line)
```

with:

```python
        try:
            coef = await self._current_odds(game_id, line, bet_type)
```

Replace the `"Type": TOTAL_OVER_T,` line inside the request body with:

```python
                    "Type": bet_type,
```

Replace the `_current_odds` method:

```python
    async def _current_odds(self, game_id: int, line: float) -> float | None:
        resp = await self._client.get(
            "/LiveFeed/GetGameZip", params={"id": game_id, "lng": "en"}
        )
        resp.raise_for_status()
        detail = resp.json().get("Value")
        if not detail:
            return None
        for event in detail.get("E") or []:
            if (
                event.get("T") == TOTAL_OVER_T
                and event.get("P") == line
                and event.get("G") == TOTALS_GROUP
            ):
                return event.get("C")
        return None
```

with:

```python
    async def _current_odds(self, game_id: int, line: float, bet_type: int) -> float | None:
        resp = await self._client.get(
            "/LiveFeed/GetGameZip", params={"id": game_id, "lng": "en"}
        )
        resp.raise_for_status()
        detail = resp.json().get("Value")
        if not detail:
            return None
        for event in detail.get("E") or []:
            if (
                event.get("T") == bet_type
                and event.get("P") == line
                and event.get("G") == TOTALS_GROUP
            ):
                return event.get("C")
        return None
```

- [ ] **Step 4: Run the tests to verify they all pass**

Run: `.venv/bin/pytest tests/test_betting_api.py -v`
Expected: all tests pass (7 original + 2 new = 9).

- [ ] **Step 5: Commit**

```bash
git add services/bettor/betting_api.py tests/test_betting_api.py
git commit -m "$(cat <<'EOF'
Extend BetExecutor.place_bet with period/over parameters

Adds period: Literal[1, 2] = 1 (selects match_id + FIRST_HALF_ID_OFFSET
vs the new SECOND_HALF_ID_OFFSET = 2) and over: bool = True (selects
TOTAL_OVER_T vs TOTAL_UNDER_T). Both default to Pattern 1's exact
existing request shape -- its call sites need no changes. Pattern 2 will
call place_bet(..., period=2, over=False).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016NyjMseHDRFt6oRPAAK1X1
EOF
)"
```

---

## Task 3: Parameterize `TargetTracker`'s staleness set + add `mutual_exclusion_reason()`

**Files:**
- Modify: `services/bettor/targeting.py`
- Modify: `tests/test_targeting.py`

**Interfaces:**
- Produces: `TargetTracker(stale_statuses: set[str] | None = None)` (default unchanged: `{"half_time", "finished"}`) and a new module-level `mutual_exclusion_reason(match_id: int, other_pattern_name: str, other_bet_targets: set[int]) -> str | None`. Both consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_targeting.py`, first updating the import line:

```python
from shared.events import MatchDiscovered
from services.bettor.targeting import TargetTracker, mutual_exclusion_reason
```

Then append:

```python
def test_default_stale_statuses_unchanged_half_time_is_stale():
    tracker = TargetTracker()  # default: {"half_time", "finished"}
    tracker.on_discovered(_discovered(10))
    tracker.on_started(10)
    tracker.on_half_time(10)
    assert tracker.is_stale(10) is True


def test_custom_stale_statuses_half_time_is_not_stale():
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(11))
    tracker.on_started(11)
    tracker.on_half_time(11)
    assert tracker.is_stale(11) is False


def test_custom_stale_statuses_finished_is_still_stale():
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(12))
    tracker.on_finished(12)
    assert tracker.is_stale(12) is True


def test_custom_stale_statuses_arm_targets_a_match_already_at_half_time():
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(13))
    tracker.on_started(13)
    tracker.on_half_time(13)  # not stale for this tracker's definition
    target = tracker.arm()
    assert target is not None
    assert target.match_id == 13
    assert 13 in tracker.bet_targets


def test_mutual_exclusion_reason_none_when_match_not_claimed_by_other():
    assert mutual_exclusion_reason(1, "pattern2", set()) is None
    assert mutual_exclusion_reason(1, "pattern2", {2, 3}) is None


def test_mutual_exclusion_reason_set_when_match_already_claimed_by_other():
    reason = mutual_exclusion_reason(1, "pattern2", {1, 2})
    assert reason == "mutual exclusion: match 1 already targeted by pattern2"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_targeting.py -v`
Expected: the new tests fail — the `stale_statuses` ones with `TypeError: __init__() got an unexpected keyword argument 'stale_statuses'`, the `mutual_exclusion_reason` ones with `ImportError: cannot import name 'mutual_exclusion_reason'`. All other existing tests still pass.

- [ ] **Step 3: Implement both changes**

In `services/bettor/targeting.py`, replace:

```python
_STALE_STATUSES = {"half_time", "finished"}


class TargetTracker:
    def __init__(self) -> None:
        self._latest_discovered: MatchDiscovered | None = None
        self._status: dict[int, str] = {}
        self._pending = False
        self.bet_targets: set[int] = set()
```

with:

```python
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
        self._stale_statuses = stale_statuses if stale_statuses is not None else _DEFAULT_STALE_STATUSES
        self.bet_targets: set[int] = set()
```

Replace both remaining references to the old module-level constant:

```python
        if (
            latest is not None
            and latest.match_id not in self.bet_targets
            and self._status.get(latest.match_id) not in _STALE_STATUSES
        ):
```

with:

```python
        if (
            latest is not None
            and latest.match_id not in self.bet_targets
            and self._status.get(latest.match_id) not in self._stale_statuses
        ):
```

and:

```python
        return self._status.get(match_id) in _STALE_STATUSES
```

with:

```python
        return self._status.get(match_id) in self._stale_statuses
```

Add the new module-level function at the end of the file:

```python
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
```

- [ ] **Step 4: Run the tests to verify they all pass**

Run: `.venv/bin/pytest tests/test_targeting.py -v`
Expected: all tests pass (9 original + 6 new = 15).

- [ ] **Step 5: Commit**

```bash
git add services/bettor/targeting.py tests/test_targeting.py
git commit -m "$(cat <<'EOF'
Parameterize TargetTracker's staleness set; add mutual_exclusion_reason()

stale_statuses defaults to {"half_time", "finished"} (Pattern 1's exact
existing behavior, unchanged), but Pattern 2 needs {"finished"} only --
half-time is when its 2nd-half market opens, not closes. Also adds a
pure mutual_exclusion_reason() helper so main.py can stop two patterns
both betting on the same match in the same round, without either
TargetTracker instance needing to know a second pattern exists.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016NyjMseHDRFt6oRPAAK1X1
EOF
)"
```

---

## Task 4: New `PATTERN2_*` config knobs

**Files:**
- Modify: `shared/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `Config.pattern2_high_threshold: int`, `Config.pattern2_streak_length: int`, `Config.pattern2_bet_line: float`, `Config.pattern2_bet_stake_amount: float` — consumed by Task 6.

- [ ] **Step 1: Add the new fields to the `Config` dataclass**

In `shared/config.py`, replace:

```python
    pattern_bet_line: float
    cdp_url: str
```

with:

```python
    pattern_bet_line: float
    pattern2_high_threshold: int
    pattern2_streak_length: int
    pattern2_bet_line: float
    pattern2_bet_stake_amount: float
    cdp_url: str
```

- [ ] **Step 2: Populate them in `load_config()`**

Replace:

```python
        pattern_bet_line=float(os.environ.get("PATTERN_BET_LINE", "6.5")),
        cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
```

with:

```python
        pattern_bet_line=float(os.environ.get("PATTERN_BET_LINE", "6.5")),
        pattern2_high_threshold=int(os.environ.get("PATTERN2_HIGH_THRESHOLD", "8")),
        pattern2_streak_length=int(os.environ.get("PATTERN2_STREAK_LENGTH", "3")),
        pattern2_bet_line=float(os.environ.get("PATTERN2_BET_LINE", "7.5")),
        pattern2_bet_stake_amount=float(os.environ.get("PATTERN2_BET_STAKE_AMOUNT", "90")),
        cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
```

- [ ] **Step 3: Verify defaults load cleanly**

Run: `.venv/bin/python -c "from shared.config import load_config; c = load_config(); print(c.pattern2_high_threshold, c.pattern2_streak_length, c.pattern2_bet_line, c.pattern2_bet_stake_amount)"`
Expected: `8 3 7.5 90.0`

- [ ] **Step 4: Document the new variables in `.env.example`**

Append to `.env.example`, right after the existing Pattern 1 section:

```bash
# --- Betting: Pattern 2 — "2nd Half Under 7.5" streak (services/bettor) ---
# Mirrors Pattern 1 in the opposite direction: fires after
# PATTERN2_STREAK_LENGTH consecutive finished rounds each have a 2nd-half
# combined goal total >= PATTERN2_HIGH_THRESHOLD; bets on the next round's
# Total. 2nd half market, Under PATTERN2_BET_LINE. Real money, live from
# the first run -- see README.md "Betting patterns" before changing these.
# Runs as a second, independent pattern in the same services/bettor
# process as Pattern 1 -- no separate service to start.
PATTERN2_HIGH_THRESHOLD=8
PATTERN2_STREAK_LENGTH=3
PATTERN2_BET_LINE=7.5
PATTERN2_BET_STAKE_AMOUNT=90
```

- [ ] **Step 5: Commit**

```bash
git add shared/config.py .env.example
git commit -m "$(cat <<'EOF'
Add config knobs for the 2nd-half-under streak betting pattern

PATTERN2_HIGH_THRESHOLD, PATTERN2_STREAK_LENGTH, PATTERN2_BET_LINE,
PATTERN2_BET_STAKE_AMOUNT -- same load_config()/.env convention as
Pattern 1's own knobs, fully independent of them.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016NyjMseHDRFt6oRPAAK1X1
EOF
)"
```

---

## Task 5: Generalize bettor event schemas + update Pattern 1's existing producers/consumers

**This is the one task that touches already-shipped, currently-live event
shapes.** It must leave the working tree fully consistent at the end —
`shared/events.py`, `services/bettor/main.py`, and `services/display/
render.py` all updated together — since this is real-money software that
could otherwise be left in a broken intermediate state.

**Files:**
- Modify: `shared/events.py`
- Modify: `services/bettor/main.py`
- Modify: `services/display/render.py`

**Interfaces:**
- Produces: updated `PatternArmed` (+`market_label: str`, +`condition_label: str`), `PatternProgress` (`low_threshold`→`threshold`, +`pattern_name: str`, +`direction: Literal["at_or_under", "at_or_over"]`), `BetPlaced` (+`market_label: str`), `BetFailed` (+`market_label: str | None = None`), `BetSettled` (`first_half_total`→`period_total`, +`market_label: str`). No new event *kinds* — `MatchEvent`/`MATCH_EVENT_TYPES` unchanged. Also produces `_market_label(period, over, line) -> str` and `_condition_label(direction, period, threshold, streak_length) -> str` helpers in `main.py`, and a `PATTERN1_NAME` constant — both consumed by Task 6.

- [ ] **Step 1: Update the event models in `shared/events.py`**

Replace:

```python
class PatternArmed(BaseModel):
    kind: Literal["pattern_armed"] = "pattern_armed"
    pattern_name: str
    qualifying_totals: list[int]


class PatternProgress(BaseModel):
    """Emitted after every finished round is fed into the streak tracker,
    whether or not it moved the streak toward firing — lets a display
    narrate the pattern's life cycle round by round instead of only at
    the moment it fires (see PatternArmed for that moment)."""

    kind: Literal["pattern_progress"] = "pattern_progress"
    match_id: int
    streak: int
    streak_length: int
    low_threshold: int
    total: int | None
    outcome: Literal["qualifying", "reset", "skipped"]


class BetPlaced(BaseModel):
    kind: Literal["bet_placed"] = "bet_placed"
    match_id: int
    league_name: str
    home: str
    away: str
    stake: float
    line: float
    odds: float | None = None


class BetFailed(BaseModel):
    kind: Literal["bet_failed"] = "bet_failed"
    match_id: int | None = None
    reason: str


class BetSettled(BaseModel):
    kind: Literal["bet_settled"] = "bet_settled"
    match_id: int
    home: str
    away: str
    won: bool
    first_half_total: int
```

with:

```python
class PatternArmed(BaseModel):
    kind: Literal["pattern_armed"] = "pattern_armed"
    pattern_name: str
    qualifying_totals: list[int]
    market_label: str  # e.g. "Total. 2nd half Under 7.5" — what's about to be bet on
    condition_label: str  # e.g. "3 consecutive rounds with 2nd half total >= 8"


class PatternProgress(BaseModel):
    """Emitted after every finished round is fed into the streak tracker,
    whether or not it moved the streak toward firing — lets a display
    narrate the pattern's life cycle round by round instead of only at
    the moment it fires (see PatternArmed for that moment)."""

    kind: Literal["pattern_progress"] = "pattern_progress"
    match_id: int
    pattern_name: str
    direction: Literal["at_or_under", "at_or_over"]
    streak: int
    streak_length: int
    threshold: int
    total: int | None
    outcome: Literal["qualifying", "reset", "skipped"]


class BetPlaced(BaseModel):
    kind: Literal["bet_placed"] = "bet_placed"
    match_id: int
    league_name: str
    home: str
    away: str
    stake: float
    line: float
    market_label: str
    odds: float | None = None


class BetFailed(BaseModel):
    kind: Literal["bet_failed"] = "bet_failed"
    match_id: int | None = None
    reason: str
    market_label: str | None = None  # None only when the failure happens before any market is known (e.g. an auth-read failure)


class BetSettled(BaseModel):
    kind: Literal["bet_settled"] = "bet_settled"
    match_id: int
    home: str
    away: str
    won: bool
    period_total: int
    market_label: str
```

- [ ] **Step 2: Sanity-check the models round-trip through JSON with the new/renamed fields**

Run:
```bash
.venv/bin/python -c "
from shared.events import BetSettled, PatternProgress
e = BetSettled(match_id=1, home='A', away='B', won=True, period_total=9, market_label='Total. 2nd half Under 7.5')
print(BetSettled.model_validate_json(e.model_dump_json()))
p = PatternProgress(match_id=1, pattern_name='x', direction='at_or_over', streak=1, streak_length=3, threshold=8, total=9, outcome='qualifying')
print(PatternProgress.model_validate_json(p.model_dump_json()))
"
```
Expected: both reconstructed objects print, no exception.

- [ ] **Step 3: Update `services/bettor/main.py`'s Pattern 1 event-construction call sites**

Add two small label-building helpers and a name constant right after the imports in `services/bettor/main.py`:

```python
PATTERN1_NAME = "1st_half_over_6.5_streak"


def _market_label(period: int, over: bool, line: float) -> str:
    half = "1st half" if period == 1 else "2nd half"
    side = "Over" if over else "Under"
    return f"Total. {half} {side} {line:g}"


def _condition_label(direction: str, period: int, threshold: int, streak_length: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    cmp = "<=" if direction == "at_or_under" else ">="
    return f"{streak_length} consecutive rounds with {half} total {cmp} {threshold}"
```

Replace the `PatternProgress` construction:

```python
                    else:
                        await bus.publish(
                            config.channel_match_events,
                            PatternProgress(
                                match_id=event.match_id,
                                streak=tracker.streak,
                                streak_length=config.pattern_streak_length,
                                low_threshold=config.pattern_low_threshold,
                                total=tracker.last_total,
                                outcome=tracker.last_outcome,
                            ),
                        )
```

with:

```python
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
```

Replace the `BetSettled` construction:

```python
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=first_half_total > config.pattern_bet_line,
                                first_half_total=first_half_total,
                            ),
                        )
```

with:

```python
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
```

Replace the `PatternArmed` construction:

```python
                        await bus.publish(
                            config.channel_match_events,
                            PatternArmed(
                                pattern_name="1st_half_over_6.5_streak",
                                qualifying_totals=list(tracker.last_streak_totals),
                            ),
                        )
```

with:

```python
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
```

In the `place()` function, replace:

```python
    async def place(target: MatchDiscovered) -> None:
        if targets.is_stale(target.match_id):
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                "already past 1st half by the time the bet was attempted"
            )
            log.warning(reason)
            await bus.publish(config.channel_match_events, BetFailed(match_id=target.match_id, reason=reason))
            return

        log.info(
            f"placing bet: {config.bet_stake_amount:g} on {target.home} vs {target.away} "
            f"(match {target.match_id}) Total. 1st half Over {config.pattern_bet_line:g}"
        )
        result = await executor.place_bet(
            match_id=target.match_id,
            home=target.home,
            away=target.away,
            stake=config.bet_stake_amount,
            line=config.pattern_bet_line,
        )
        if result.success:
            log.info(f"bet placed on match {target.match_id} at odds {result.odds}")
            await bus.publish(
                config.channel_match_events,
                BetPlaced(
                    match_id=target.match_id,
                    league_name=target.league_name,
                    home=target.home,
                    away=target.away,
                    stake=config.bet_stake_amount,
                    line=config.pattern_bet_line,
                    odds=result.odds,
                ),
            )
        else:
            log.error(f"bet failed for match {target.match_id}: {result.reason}")
            await bus.publish(config.channel_match_events, BetFailed(match_id=target.match_id, reason=result.reason))
```

with:

```python
    async def place(target: MatchDiscovered) -> None:
        market_label = _market_label(1, True, config.pattern_bet_line)

        if targets.is_stale(target.match_id):
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                "already past 1st half by the time the bet was attempted"
            )
            log.warning(reason)
            await bus.publish(
                config.channel_match_events,
                BetFailed(match_id=target.match_id, reason=reason, market_label=market_label),
            )
            return

        log.info(
            f"placing bet: {config.bet_stake_amount:g} on {target.home} vs {target.away} "
            f"(match {target.match_id}) {market_label}"
        )
        result = await executor.place_bet(
            match_id=target.match_id,
            home=target.home,
            away=target.away,
            stake=config.bet_stake_amount,
            line=config.pattern_bet_line,
        )
        if result.success:
            log.info(f"bet placed on match {target.match_id} at odds {result.odds}")
            await bus.publish(
                config.channel_match_events,
                BetPlaced(
                    match_id=target.match_id,
                    league_name=target.league_name,
                    home=target.home,
                    away=target.away,
                    stake=config.bet_stake_amount,
                    line=config.pattern_bet_line,
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
```

- [ ] **Step 4: Update `services/display/render.py`'s render functions to print the new fields verbatim**

Replace:

```python
def render_pattern_armed(event: PatternArmed) -> None:
    totals = ", ".join(str(t) for t in event.qualifying_totals)
    console.print(
        Panel(
            f"3 consecutive rounds at or under the threshold: {totals}\n"
            "Betting on the next round's 1st-half Over line.",
            title="[magenta]◈ PATTERN FIRED[/magenta]  1st Half Over — streak",
            border_style="magenta",
            title_align="left",
        )
    )


def render_pattern_progress(event: PatternProgress) -> None:
    """One dim line narrating what the just-finished round did to the
    streak — printed for every round (see render_pattern_armed for the
    moment it actually fires, which this deliberately doesn't duplicate)."""
    this_round = f"this round: {event.total}" if event.total is not None else "this round: unknown"
    if event.outcome == "reset":
        detail = f"{this_round} > {event.low_threshold}, resets" if event.total is not None else f"{this_round}, resets"
    elif event.outcome == "skipped":
        detail = f"{this_round}, skipped — bet target"
    else:  # qualifying
        detail = f"{this_round} ≤ {event.low_threshold}, qualifies"
    console.print(f"[bold bright_red]streak: {event.streak}/{event.streak_length} ({detail})[/bold bright_red]")


def render_bet_placed(event: BetPlaced) -> None:
    odds_str = f" @ {event.odds:.2f}" if event.odds is not None else ""
    console.print(
        f"[green]✓ BET PLACED[/green]  {event.stake:g} on {event.home} vs {event.away} "
        f"— Total. 1st half Over {event.line:g}{odds_str}"
    )


def render_bet_failed(event: BetFailed) -> None:
    where = f" (match {event.match_id})" if event.match_id is not None else ""
    console.print(f"[red]✗ BET FAILED[/red]{where}  {event.reason}")


def render_bet_settled(event: BetSettled) -> None:
    label = "[bold green]WON[/bold green]" if event.won else "[bold red]LOST[/bold red]"
    console.print(
        f"[cyan]● SETTLED[/cyan]  {event.home} vs {event.away} "
        f"— 1st half total {event.first_half_total} — {label}"
    )
```

with:

```python
def render_pattern_armed(event: PatternArmed) -> None:
    totals = ", ".join(str(t) for t in event.qualifying_totals)
    console.print(
        Panel(
            f"{event.condition_label}\n"
            f"Qualifying totals: {totals}\n"
            f"Betting on the next round's {event.market_label}.",
            title=f"[magenta]◈ PATTERN FIRED[/magenta]  {event.market_label}",
            border_style="magenta",
            title_align="left",
        )
    )


def render_pattern_progress(event: PatternProgress) -> None:
    """One dim line narrating what the just-finished round did to the
    streak — printed for every round (see render_pattern_armed for the
    moment it actually fires, which this deliberately doesn't duplicate).
    `direction` picks the qualify/reset comparison wording so this reads
    correctly for both Pattern 1 (at_or_under) and Pattern 2 (at_or_over)."""
    qualify_cmp = "≤" if event.direction == "at_or_under" else "≥"
    reset_cmp = ">" if event.direction == "at_or_under" else "<"
    this_round = f"this round: {event.total}" if event.total is not None else "this round: unknown"
    if event.outcome == "reset":
        detail = (
            f"{this_round} {reset_cmp} {event.threshold}, resets"
            if event.total is not None
            else f"{this_round}, resets"
        )
    elif event.outcome == "skipped":
        detail = f"{this_round}, skipped — bet target"
    else:  # qualifying
        detail = f"{this_round} {qualify_cmp} {event.threshold}, qualifies"
    console.print(
        f"[bold bright_red]{event.pattern_name} streak: {event.streak}/{event.streak_length} ({detail})[/bold bright_red]"
    )


def render_bet_placed(event: BetPlaced) -> None:
    odds_str = f" @ {event.odds:.2f}" if event.odds is not None else ""
    console.print(
        f"[green]✓ BET PLACED[/green]  {event.stake:g} on {event.home} vs {event.away} "
        f"— {event.market_label}{odds_str}"
    )


def render_bet_failed(event: BetFailed) -> None:
    where = f" (match {event.match_id})" if event.match_id is not None else ""
    market = f" [{event.market_label}]" if event.market_label is not None else ""
    console.print(f"[red]✗ BET FAILED[/red]{where}{market}  {event.reason}")


def render_bet_settled(event: BetSettled) -> None:
    label = "[bold green]WON[/bold green]" if event.won else "[bold red]LOST[/bold red]"
    console.print(
        f"[cyan]● SETTLED[/cyan]  {event.home} vs {event.away} "
        f"— {event.market_label} total {event.period_total} — {label}"
    )
```

Replace `log_bet_event`:

```python
def log_bet_event(event: BettorEvent, target: Console) -> None:
    """Plain-text twin of the four render_* functions above, appended to
    data/bets.log — same convention as log_finished()/data/result.log."""
    target.print(_datetime(time.time()))
    if isinstance(event, PatternArmed):
        target.print(f"PATTERN ARMED — streak {event.qualifying_totals}")
    elif isinstance(event, BetPlaced):
        odds_str = f" @ {event.odds:.2f}" if event.odds is not None else ""
        target.print(
            f"BET PLACED — {event.stake:g} on {event.home} vs {event.away} "
            f"(match {event.match_id}) Total. 1st half Over {event.line:g}{odds_str}"
        )
    elif isinstance(event, BetFailed):
        target.print(f"BET FAILED — match {event.match_id}: {event.reason}")
    elif isinstance(event, BetSettled):
        outcome = "WON" if event.won else "LOST"
        target.print(
            f"SETTLED — {event.home} vs {event.away}: {outcome} "
            f"(1st half total {event.first_half_total})"
        )
    target.print()
```

with:

```python
def log_bet_event(event: BettorEvent, target: Console) -> None:
    """Plain-text twin of the four render_* functions above, appended to
    data/bets.log — same convention as log_finished()/data/result.log."""
    target.print(_datetime(time.time()))
    if isinstance(event, PatternArmed):
        target.print(f"PATTERN ARMED — {event.market_label} — streak {event.qualifying_totals}")
    elif isinstance(event, BetPlaced):
        odds_str = f" @ {event.odds:.2f}" if event.odds is not None else ""
        target.print(
            f"BET PLACED — {event.stake:g} on {event.home} vs {event.away} "
            f"(match {event.match_id}) {event.market_label}{odds_str}"
        )
    elif isinstance(event, BetFailed):
        market = f" [{event.market_label}]" if event.market_label is not None else ""
        target.print(f"BET FAILED{market} — match {event.match_id}: {event.reason}")
    elif isinstance(event, BetSettled):
        outcome = "WON" if event.won else "LOST"
        target.print(
            f"SETTLED — {event.home} vs {event.away}: {outcome} "
            f"({event.market_label} total {event.period_total})"
        )
    target.print()
```

- [ ] **Step 5: Smoke-test both modules still import and construct cleanly**

Run: `.venv/bin/python -c "import services.bettor.main; import services.display.render"`
Expected: no exception.

Run the full existing test suite to confirm nothing else broke:
Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add shared/events.py services/bettor/main.py services/display/render.py
git commit -m "$(cat <<'EOF'
Generalize bettor event schemas beyond Pattern 1's hardcoded shapes

BetPlaced/BetFailed/PatternArmed/BetSettled gain market_label (built
once per pattern in main.py, printed verbatim by render.py instead of
hardcoded "1st half" text); PatternArmed gains condition_label;
BetSettled.first_half_total is renamed period_total; PatternProgress
gains pattern_name/direction (and low_threshold is renamed threshold)
so its render can pick correct <=/>= wording per pattern. No new event
kinds -- existing producers/consumers updated in the same commit so the
tree stays consistent. This unblocks Pattern 2 needing no render.py
changes of its own.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016NyjMseHDRFt6oRPAAK1X1
EOF
)"
```

---

## Task 6: Wire Pattern 2 into `services/bettor/main.py`

**Files:**
- Modify: `services/bettor/main.py`

**Interfaces:**
- Consumes: `PatternTracker(direction=...)` (Task 1), `BetExecutor.place_bet(period=..., over=...)` (Task 2), `TargetTracker(stale_statuses=...)`/`mutual_exclusion_reason()` (Task 3), `config.pattern2_*` (Task 4), generalized events + `_market_label`/`_condition_label`/`PATTERN1_NAME` (Task 5).
- Produces: the full two-pattern `consume()` loop — `python -m services.bettor.main` now runs both patterns.

- [ ] **Step 1: Update imports**

Replace:

```python
from services.bettor.betting_api import BetExecutor
from services.bettor.pattern import PatternTracker
from services.bettor.targeting import TargetTracker
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
```

with:

```python
from services.bettor.betting_api import BetExecutor
from services.bettor.pattern import PatternTracker
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

PATTERN2_NAME = "2nd_half_under_7.5_streak"
```

(`PATTERN1_NAME` already exists from Task 5, right below the imports — leave it in place.)

- [ ] **Step 2: Construct Pattern 2's tracker and target-tracker instances**

Replace:

```python
    tracker = PatternTracker(threshold=config.pattern_low_threshold, streak_length=config.pattern_streak_length)
    targets = TargetTracker()
    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )
```

with:

```python
    tracker = PatternTracker(threshold=config.pattern_low_threshold, streak_length=config.pattern_streak_length)
    targets = TargetTracker()

    tracker2 = PatternTracker(
        threshold=config.pattern2_high_threshold,
        streak_length=config.pattern2_streak_length,
        direction="at_or_over",
    )
    targets2 = TargetTracker(stale_statuses={"finished"})

    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting Pattern 1 — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )
    log.info(
        f"starting Pattern 2 — streak_length={config.pattern2_streak_length} "
        f"high_threshold={config.pattern2_high_threshold} bet_line={config.pattern2_bet_line} "
        f"stake={config.pattern2_bet_stake_amount}"
    )
```

- [ ] **Step 3: Replace `place()` with a parameterized version shared by both patterns**

Replace the entire `place()` function (as it stands after Task 5's edits) with:

```python
    async def place(
        target: MatchDiscovered,
        *,
        targets: TargetTracker,
        other_targets: TargetTracker,
        other_pattern_name: str,
        period: int,
        over: bool,
        line: float,
        stake: float,
    ) -> None:
        market_label = _market_label(period, over, line)

        conflict = mutual_exclusion_reason(target.match_id, other_pattern_name, other_targets.bet_targets)
        if conflict is not None:
            log.warning(conflict)
            await bus.publish(
                config.channel_match_events,
                BetFailed(match_id=target.match_id, reason=conflict, market_label=market_label),
            )
            return

        if targets.is_stale(target.match_id):
            half = "1st half" if period == 1 else "2nd half"
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                f"already past its {half} by the time the bet was attempted"
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
        result = await executor.place_bet(
            match_id=target.match_id,
            home=target.home,
            away=target.away,
            stake=stake,
            line=line,
            period=period,
            over=over,
        )
        if result.success:
            log.info(f"bet placed on match {target.match_id} at odds {result.odds}")
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
```

- [ ] **Step 4: Update the `PatternArmed`/`PatternProgress`/`BetSettled` construction to pass through `place()`'s new signature, and add Pattern 2's own MatchFinished-triggered evaluation**

Replace the whole `consume()` function with:

```python
    async def consume() -> None:
        async for event in bus.subscribe_match_events(config.channel_match_events):
            try:
                if isinstance(event, MatchDiscovered):
                    target = targets.on_discovered(event)
                    if target is not None:
                        await place(
                            target,
                            targets=targets,
                            other_targets=targets2,
                            other_pattern_name=PATTERN2_NAME,
                            period=1,
                            over=True,
                            line=config.pattern_bet_line,
                            stake=config.bet_stake_amount,
                        )
                    target2 = targets2.on_discovered(event)
                    if target2 is not None:
                        await place(
                            target2,
                            targets=targets2,
                            other_targets=targets,
                            other_pattern_name=PATTERN1_NAME,
                            period=2,
                            over=False,
                            line=config.pattern2_bet_line,
                            stake=config.pattern2_bet_stake_amount,
                        )
                elif isinstance(event, MatchStarted):
                    targets.on_started(event.match_id)
                    targets2.on_started(event.match_id)
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                    targets2.on_half_time(event.match_id)

                    first_half_total = event.first_half.home_goals + event.first_half.away_goals

                    if event.match_id in targets.bet_targets:
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
                                other_targets=targets2,
                                other_pattern_name=PATTERN2_NAME,
                                period=1,
                                over=True,
                                line=config.pattern_bet_line,
                                stake=config.bet_stake_amount,
                            )
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
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)
                    targets2.on_finished(event.match_id)

                    second_half_total = (
                        None if event.second_half is None
                        else event.second_half.home_goals + event.second_half.away_goals
                    )

                    if event.match_id in targets2.bet_targets and second_half_total is not None:
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
                                other_targets=targets,
                                other_pattern_name=PATTERN1_NAME,
                                period=2,
                                over=False,
                                line=config.pattern2_bet_line,
                                stake=config.pattern2_bet_stake_amount,
                            )
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
            except Exception as err:  # noqa: BLE001 — one bad event must not kill the subscription
                log.error(f"failed to process {event.kind}: {err}")
```

Notes on this step, for the executor's own understanding (not further action needed):
- `MatchDiscovered` now resolves *both* trackers' pending targets — each independently, since each `TargetTracker` has its own `_pending` flag.
- `MatchStarted`/`MatchHalfTime`/`MatchFinished` all update *both* trackers' status bookkeeping (`on_started`/`on_half_time`/`on_finished`), regardless of which pattern's streak they also happen to evaluate — status tracking is needed by both regardless of which one currently has a pending target.
- Pattern 1's streak evaluation + settlement stays on `MatchHalfTime` (unchanged from Task 5). Pattern 2's streak evaluation + settlement is new, on `MatchFinished`, gated on `event.second_half is not None` (mirrors Pattern 1's existing `event.first_half is not None`-style guard pattern, here expressed as computing `second_half_total = None` when `event.second_half` is `None` and letting `PatternTracker.process(None)` reset the streak exactly like an unknown 1st-half total does for Pattern 1).
- The mutual-exclusion check lives inside `place()` (Task 5/6's shared helper), so it applies uniformly whether a target resolves immediately (via `arm()`) or later (via a pending `on_discovered()` resolution) — both code paths call `place()`.

- [ ] **Step 5: Smoke-test the module still imports and constructs cleanly**

Run: `.venv/bin/python -c "import services.bettor.main"`
Expected: no exception.

Run the full test suite one more time:
Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass (unchanged from Task 5 — `main.py` itself has no direct unit tests, consistent with Pattern 1's own convention; it's verified by the smoke-import above and the live trial in Task 8).

- [ ] **Step 6: Commit**

```bash
git add services/bettor/main.py
git commit -m "$(cat <<'EOF'
Wire Pattern 2 into services/bettor/main.py

A second, parallel PatternTracker/TargetTracker pair evaluates 2nd-half
totals at MatchFinished (the only event carrying a final 2nd-half
score) and bets Total. 2nd half Under via BetExecutor(period=2,
over=False). place() is now shared and parameterized by both patterns,
and checks mutual_exclusion_reason() against the other pattern's
TargetTracker before ever calling the executor, so the two patterns
never both stake money on the same match in the same round.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016NyjMseHDRFt6oRPAAK1X1
EOF
)"
```

---

## Task 7: Document Pattern 2 in `README.md`

**Files:**
- Modify: `README.md`

**Interfaces:** none (documentation only).

- [ ] **Step 1: Add a "Pattern 2" subsection under "Betting patterns"**

In `README.md`, immediately after the existing Pattern 1 subsection (right before the `## How data is sourced` heading), add:

```markdown
### Pattern 2 — "2nd Half Under 7.5" streak

The mirror image of Pattern 1, run as a second, independent pattern in
the same `services/bettor/` process — no separate service, no separate
CDP session. It watches every finished round's **2nd-half** combined
goal total instead of the 1st-half's. 3 consecutive rounds at or above
`PATTERN2_HIGH_THRESHOLD` (default 8) fire a real bet —
`PATTERN2_BET_STAKE_AMOUNT` (default 90, FCFA, independently
configurable from Pattern 1's stake) on the *next* round's `Total. 2nd
half` market, `Under PATTERN2_BET_LINE` (default 7.5). Same fire → skip
one round → restart life cycle as Pattern 1, evaluated in the opposite
direction.

Because a final 2nd-half score is only known once a round actually
finishes (unlike the 1st half, whose score is known at half-time),
Pattern 2 evaluates at each round's `MatchFinished` event rather than
`MatchHalfTime` — one event later in the round's lifecycle than
Pattern 1.

**Mutual exclusion:** since both patterns watch the same "next match to
kick off," they can resolve to targeting the same match in the same
round. Only one bet per match is ever placed — whichever pattern's
target resolves first wins it; the other logs a `BET FAILED` with a
`mutual exclusion: ...` reason instead of also staking money on it.

Config knobs: `PATTERN2_HIGH_THRESHOLD`, `PATTERN2_STREAK_LENGTH`,
`PATTERN2_BET_LINE`, `PATTERN2_BET_STAKE_AMOUNT` — see `.env.example`.
Shares `CDP_URL` and `BETS_LOG_PATH` with Pattern 1.
```

- [ ] **Step 2: Update the Event catalogue table's field descriptions**

The four existing bettor-event rows already list only a sample of fields (`match_id`, `stake`, `line`, `odds`, etc.) rather than every field — leave the table rows as-is; the new `market_label`/`condition_label`/`period_total`/`pattern_name`/`direction` fields don't need their own table row edits, since the table was already a representative sample, not an exhaustive schema (the models themselves in `shared/events.py` are the source of truth). Skip this step's edit — noted here only so it's not mistaken for an oversight.

- [ ] **Step 3: Update the Roadmap section**

Replace:

```markdown
## Roadmap

Extraction + real-time terminal display, plus one live betting pattern
(see [Betting patterns](#betting-patterns)), are both implemented. Later
patterns build on the same event bus — each is just another subscriber to
`xbet.match_events` publishing its own `PatternArmed`/`Bet*` events, the
same shape `services/bettor/` already follows, described in
[Architecture](#architecture).
```

with:

```markdown
## Roadmap

Extraction + real-time terminal display, plus two live betting patterns
(see [Betting patterns](#betting-patterns)), are all implemented. Both
patterns run in the same `services/bettor/` process, coordinated so they
never both bet on the same match in the same round. Any future pattern
follows the same shape: extend `PatternTracker`/`TargetTracker`/
`BetExecutor` rather than forking them, and publish its own
`PatternArmed`/`Bet*` events onto the same `xbet.match_events` bus.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document Pattern 2 in README

Adds a "Pattern 2" subsection mirroring Pattern 1's, covering the
opposite-direction 2nd-half rule, the MatchFinished evaluation point,
and the mutual-exclusion guard. Updates the Roadmap to reflect both
patterns being implemented.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_016NyjMseHDRFt6oRPAAK1X1
EOF
)"
```

---

## Task 8: Full verification + live-trial authorization checkpoint

Not a code change — the final verification step before Pattern 2 is trusted to run unattended alongside Pattern 1. This is real money on the user's real account; the first bet ever placed through `period=2, over=False` needs to be watched.

**Files:** none

- [ ] **Step 1: Run the full test suite one more time, from a clean install**

```bash
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pytest tests/ -v
```

Expected: all tests pass — this now includes every test added across Tasks 1-3 on top of the pre-existing Pattern 1 suite.

- [ ] **Step 2: Smoke-check every touched module imports cleanly**

```bash
.venv/bin/python -c "
import services.bettor.main
import services.bettor.pattern
import services.bettor.targeting
import services.bettor.betting_api
import services.display.main
import services.display.render
import shared.events
import shared.config
print('all modules import cleanly')
"
```

Expected: prints `all modules import cleanly`, no exception.

- [ ] **Step 3: Confirm config defaults load as expected**

```bash
.venv/bin/python -c "
from shared.config import load_config
c = load_config()
print('Pattern 1:', c.pattern_low_threshold, c.pattern_streak_length, c.pattern_bet_line, c.bet_stake_amount)
print('Pattern 2:', c.pattern2_high_threshold, c.pattern2_streak_length, c.pattern2_bet_line, c.pattern2_bet_stake_amount)
"
```

Expected: `Pattern 1: 6 3 6.5 90.0` and `Pattern 2: 8 3 7.5 90.0` (or whatever this deployment's actual `.env` overrides say, if any are set).

- [ ] **Step 4: Ask the user before restarting the live bot**

Pattern 2's code has never run against the live event stream before this point — do not run `./run.sh restart` as part of this task without first telling the user explicitly that this will (a) restart the currently-running bot (interrupting Pattern 1's live trial for the length of the restart) and (b) bring Pattern 2's streak-tracking online from a cold start (streak 0/3, same accepted in-memory-state-loss limitation Pattern 1 already has on any restart). Get an explicit go-ahead for the restart itself, separate from having already approved the code changes.

- [ ] **Step 5: Restart and watch both patterns**

Once the user confirms:

```bash
./run.sh restart
```

Watch the terminal (`display` is attached) and `tail -f logs/bettor.log` in a second terminal for several finished rounds. Confirm:
- Both `Pattern 1 streak: N/3 (...)` and `2nd_half_under_7.5_streak streak: N/3 (...)` progress lines appear, one pair per finished round (Pattern 1's on `MatchHalfTime`, Pattern 2's on the following `MatchFinished` for the same round).
- If either pattern's streak reaches 3, a `PATTERN FIRED` panel appears with the correct `market_label` (`Total. 1st half Over 6.5` or `Total. 2nd half Under 7.5`) and `condition_label`.

- [ ] **Step 6: Before the first real Pattern 2 bet is allowed to go through, get explicit authorization**

The moment `logs/bettor.log` shows Pattern 2's tracker about to arm and target a real match (streak at 2/3 on an `at_or_over` line, i.e. one qualifying round away from firing), stop and explicitly tell the user this is about to happen, and confirm they want the resulting bet (if the streak completes) to actually go through — consistent with CLAUDE.md's standing rule that every new *kind* of live action gets its own explicit confirmation, not just a standing approval of the code change. If they confirm, let it run and watch the resulting `BET PLACED`/`BET FAILED` panel and, once that match finishes, the `SETTLED` panel. If they'd rather not risk it yet, stop the bot (`./run.sh stop`) before the streak completes and revisit config (e.g. temporarily raising `PATTERN2_STREAK_LENGTH`, or stopping before the third qualifying round) rather than letting an unauthorized real bet go through.

- [ ] **Step 7: Confirm the mutual-exclusion guard, if the opportunity arises during the trial**

If both patterns' streaks happen to complete in the same round (targeting the same next match), confirm in `logs/bettor.log` that only one `BET PLACED` happened and the other pattern logged a `BET FAILED` with a `mutual exclusion: ...` reason, not a second real bet on the same match. This is a low-probability event during any single trial window — if it doesn't happen naturally, the unit test from Task 3 (`test_mutual_exclusion_reason_set_when_match_already_claimed_by_other`) is the authoritative coverage for this behavior; this step is a bonus real-world confirmation, not a blocking requirement.
