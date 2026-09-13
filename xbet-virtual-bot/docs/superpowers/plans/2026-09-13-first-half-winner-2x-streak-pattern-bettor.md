# Pattern 3 ("1st Half Winner 2X" streak) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third, independent automated betting pattern to the already-live `services/bettor/` process — detect 2 consecutive finished rounds whose 1st-half Double Chance result is `"2X"` (away win or draw), then place a real bet on the next round's `Double Chance. 1st half` market, selection `1X`, via the already-implemented `BetExecutor` API client.

**Architecture:** No new service. `PatternTracker` gains a third `direction="equals"` mode for categorical (rather than numeric) streak matching — the fire/skip/reset life cycle itself is unchanged, reused verbatim. `BetExecutor.place_bet()` gains optional `bet_type`/`group` parameters so it can address the Double Chance market (`Group=8`, `Type=4/5/6`) alongside the existing Total market (`Group=17`, `Type=9/10`), using the same per-half sub-game-id scheme. A new `shared/markets.py` module holds `double_chance_winner()`, moved out of `services/display/render.py` so both it and the new Pattern 3 code in `services/bettor/main.py` derive the 1st-half result the same way. `services/bettor/main.py`'s `place()` helper is generalized from a Total-shaped signature (`period`/`over`/`line`) to a market-agnostic one (`market_label`/`place_call`) so all three patterns share it, and its mutual-exclusion check is generalized from one other tracker to a list of them.

**Tech Stack:** Python 3.10, asyncio, pydantic, Redis pub/sub (existing `shared/bus.py`), `rich` (existing `services/display`), `httpx` (existing `services/bettor/betting_api.py`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-13-first-half-winner-2x-streak-pattern-bettor-design.md`

## Global Constraints

- Pattern 3: 2 consecutive finished rounds whose 1st-half Double Chance result is exactly `"2X"` fires a bet on the next round's `Double Chance. 1st half` market, selection `1X`. Streak length is `PATTERN3_STREAK_LENGTH`, default `2`.
- After a fire, the streak resets to 0 and the very next finished round is excluded from counting (it's the one just bet on), regardless of its own result — the round after that restarts the count from 0. Any round whose result is not `"2X"` (i.e. `"1X"` or `"X"`) breaks the streak with no skip. Identical life-cycle to Pattern 1/2, just evaluated on a categorical value instead of a numeric total.
- Pattern 3 evaluates at `MatchHalfTime` (not `MatchFinished`) — same event Pattern 1 already evaluates at, since the 1st-half result is fully known there.
- The Double Chance market is `Group=8` on the 1xbet JSON API; `Type=4`→`1X`, `Type=5`→`12`, `Type=6`→`2X`; `Param` is always `null` (no line, unlike Totals). It uses the same `match_id`/`match_id+1`/`match_id+2` sub-game-id scheme Totals already use — Pattern 3 always targets `match_id + FIRST_HALF_ID_OFFSET` (period 1), same as Pattern 1. Confirmed live, read-only, against `GetGameZip` — see the spec's "Market mechanics" section. Not confirmed via an actual placed bet; this is an explicitly accepted residual risk, not a gap this plan needs to close.
- Stake is `PATTERN3_BET_STAKE_AMOUNT`, default `90` (FCFA) — independently configurable from Pattern 1/2's stakes, never hardcoded at the call site.
- `PATTERN3_ENABLED` defaults to `true` — Pattern 3 goes live immediately once this plan's tasks are committed and the bot is restarted (explicit prior user decision, not revisited by this plan).
- Mutual exclusion is now three-way: no two of Pattern 1/2/3 may ever place a bet on the same `match_id` in the same round. Whichever pattern's target resolves first wins the match; the others log a `BetFailed` (reason starting `"mutual exclusion: ..."`) and do not call the executor, but their own tracker/target-tracker state still advances normally.
- Every existing Pattern 1/2 test (`tests/test_pattern.py`, `tests/test_betting_api.py`, `tests/test_targeting.py`) must still pass, **unmodified in behavior**, after every task in this plan — Pattern 1 and Pattern 2 are live, real-money software; nothing here may change their behavior. Several tasks below refactor code both patterns already depend on (`place()`, `place_bet()`) — this is a behavior-preserving refactor for them, and the existing suite passing unmodified is what proves it.

---

## Task 1: Extract `double_chance_winner()` into `shared/markets.py`

**Files:**
- Create: `shared/markets.py`
- Create: `tests/test_markets.py`
- Modify: `services/display/render.py`

**Interfaces:**
- Produces: `double_chance_winner(home_goals: int, away_goals: int) -> str` (returns `"1X"`, `"2X"`, or `"X"`). Consumed by Task 6 (Pattern 3's streak input) and by `services/display/render.py`'s existing `_result_table()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_markets.py`:

```python
from shared.markets import double_chance_winner


def test_home_win_is_1x():
    assert double_chance_winner(2, 0) == "1X"


def test_away_win_is_2x():
    assert double_chance_winner(0, 2) == "2X"


def test_draw_is_x():
    assert double_chance_winner(1, 1) == "X"


def test_zero_zero_draw_is_x():
    assert double_chance_winner(0, 0) == "X"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_markets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.markets'`.

- [ ] **Step 3: Create `shared/markets.py`**

```python
"""Pure, shared market-grading helpers — no I/O, no state, reusable by any
service that needs to know how a market actually settles from raw goal
counts. Currently just Double Chance, moved here from
services/display/render.py so services/bettor/main.py (Pattern 3) can
derive the same value the display already computes for its "winner for
1st/2nd half" column, without a second, potentially-drifting copy of the
same 4-line rule.
"""
from __future__ import annotations


def double_chance_winner(home_goals: int, away_goals: int) -> str:
    """Which Double Chance selection actually settles as the winner for a
    half (or the whole match), given the final score for that scope —
    "1X" (home win or draw), "2X" (away win or draw), or "X" for an
    outright draw (not itself a Double Chance selection, but the
    plain-language answer when both 1X and 2X would settle as winners).
    Derived straight from goal counts already on hand — this is exactly
    how the market grades, so there's nothing to fetch."""
    if home_goals > away_goals:
        return "1X"
    if away_goals > home_goals:
        return "2X"
    return "X"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_markets.py -v`
Expected: all 4 tests pass.

- [ ] **Step 5: Update `services/display/render.py` to use the shared function**

Replace the import block:

```python
from shared.events import (
    BetFailed,
    BetPlaced,
    BetSettled,
    HalfScore,
    MatchDiscovered,
    MatchFinished,
    MatchHalfTime,
    MatchScoreChanged,
    MatchStarted,
    MoneylineOdds,
    PatternArmed,
    PatternProgress,
)
```

with:

```python
from shared.events import (
    BetFailed,
    BetPlaced,
    BetSettled,
    HalfScore,
    MatchDiscovered,
    MatchFinished,
    MatchHalfTime,
    MatchScoreChanged,
    MatchStarted,
    MoneylineOdds,
    PatternArmed,
    PatternProgress,
)
from shared.markets import double_chance_winner
```

Replace the local function definition (and its docstring):

```python
def _double_chance_winner(home_goals: int, away_goals: int) -> str:
    """Which Double Chance selection actually settles as the winner for a
    half, given that half's final score — "1X" (home win or draw), "2X"
    (away win or draw), or "X" for an outright draw (not itself a Double
    Chance selection, but the plain-language answer when both 1X and 2X
    would settle as winners). Derived straight from the goal counts the
    collector already reports — this is exactly how the market grades, so
    there's nothing to fetch: no odds price, no CDP read, just arithmetic
    on data already on hand."""
    if home_goals > away_goals:
        return "1X"
    if away_goals > home_goals:
        return "2X"
    return "X"
```

with nothing (delete it — the docstring's content now lives in `shared/markets.py`).

Replace the two call sites:

```python
    h1_winner = None if h1_home is None or h1_away is None else _double_chance_winner(h1_home, h1_away)
    h2_winner = None if h2_home is None or h2_away is None else _double_chance_winner(h2_home, h2_away)
```

with:

```python
    h1_winner = None if h1_home is None or h1_away is None else double_chance_winner(h1_home, h1_away)
    h2_winner = None if h2_home is None or h2_away is None else double_chance_winner(h2_home, h2_away)
```

And update the one remaining reference to the old name in `_result_table`'s docstring — replace:

```python
    guessed at from a still-in-progress running total. "winner for 1st/2nd
    half" is the settled Double Chance selection for that half (see
    _double_chance_winner) — blank until that half's score is fully known,
    same gating as the "total for" columns next to it."""
```

with:

```python
    guessed at from a still-in-progress running total. "winner for 1st/2nd
    half" is the settled Double Chance selection for that half (see
    shared/markets.py's double_chance_winner) — blank until that half's
    score is fully known, same gating as the "total for" columns next to
    it."""
```

- [ ] **Step 6: Smoke-test `render.py` still imports and runs cleanly**

Run: `.venv/bin/python -c "import services.display.render"`
Expected: no exception.

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass (existing suite + 4 new).

- [ ] **Step 7: Commit**

```bash
git add shared/markets.py tests/test_markets.py services/display/render.py
git commit -m "$(cat <<'EOF'
Extract double_chance_winner() into shared/markets.py

Moved out of services/display/render.py (where it was display-only,
called _double_chance_winner) so services/bettor/main.py's upcoming
Pattern 3 can derive the same "1X"/"2X"/"X" result from a match's
1st-half goals without a second, potentially-drifting copy of the same
rule. No behavior change to render.py's output.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DMfy8WG1Kbn7KX6nf1GywZ
EOF
)"
```

---

## Task 2: Add a categorical `"equals"` direction to `PatternTracker`

**Files:**
- Modify: `services/bettor/pattern.py`
- Modify: `tests/test_pattern.py`

**Interfaces:**
- Produces: `PatternTracker(threshold: int | str = 6, streak_length: int = 3, direction: Literal["at_or_under", "at_or_over", "equals"] = "at_or_under")` — `.process(total: int | str | None) -> bool` now accepts a string total too; `.last_total`, `.last_streak_totals` are typed `int | str` accordingly. All other behavior/meaning unchanged. Consumed by Task 6 (`PatternTracker(threshold="2X", streak_length=config.pattern3_streak_length, direction="equals")`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pattern.py`:

```python
def test_equals_direction_fires_on_two_consecutive_matching_rounds():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True
    assert tracker.last_streak_totals == ["2X", "2X"]


def test_equals_direction_non_matching_round_breaks_the_streak():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("1X") is False  # breaks it -- not a match
    assert tracker.process("2X") is False  # restarts at 1
    assert tracker.process("2X") is True


def test_equals_direction_draw_does_not_qualify():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("X") is False  # a draw is not "2X" -- breaks it
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True


def test_equals_direction_none_total_breaks_the_streak():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process(None) is False
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True


def test_equals_direction_fire_then_skip_one_round_then_restart():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True  # fires

    assert tracker.process("2X") is False  # the skipped bet-target round, regardless of its own result

    assert tracker.process("2X") is False
    assert tracker.process("2X") is True
    assert tracker.last_streak_totals == ["2X", "2X"]


def test_equals_direction_progress_reporting():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")

    tracker.process("2X")
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (1, "2X", "qualifying")

    tracker.process("1X")
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, "1X", "reset")

    tracker.process("2X")
    tracker.process("2X")
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, "2X", "armed")

    tracker.process("1X")  # the skipped bet-target round
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, "1X", "skipped")


def test_numeric_directions_unaffected_by_the_equals_addition():
    # PatternTracker() and direction="at_or_over" keep working with plain
    # ints exactly as before -- this is a type-widening, not a behavior
    # change, for the two existing directions.
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True
```

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: the 6 new `equals`-based tests fail with `TypeError: '<=' not supported between instances of 'str' and 'int'` (or similar) inside `_qualifies`; `test_numeric_directions_unaffected_by_the_equals_addition` passes already (no code change needed for it, it's a regression guard); all other existing tests still pass.

- [ ] **Step 3: Implement the `"equals"` direction**

In `services/bettor/pattern.py`, replace:

```python
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
```

with:

```python
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
```

Update the module docstring's life-cycle paragraph — replace:

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
```

with:

```python
"""PatternTracker — streak-detector state machine shared by all three
betting patterns: Pattern 1 ("1st Half Over 6.5", direction="at_or_under"),
Pattern 2 ("2nd Half Under 7.5", direction="at_or_over"), and Pattern 3
("1st Half Winner 2X", direction="equals"). Pure state machine, no I/O:
fed one finished round's value (a combined goal total for Pattern 1/2, a
"1X"/"2X"/"X" Double Chance result for Pattern 3) at a time, in the order
rounds actually finish. See docs/superpowers/specs/2026-09-09-first-half-
over-pattern-bettor-design.md, docs/superpowers/specs/2026-09-10-second-
half-under-pattern-bettor-design.md, and docs/superpowers/specs/2026-09-13-
first-half-winner-2x-streak-pattern-bettor-design.md for each pattern's
full rationale; this module only encodes the shared mechanics.

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
```

- [ ] **Step 4: Run the tests to verify they all pass**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: all tests pass (previous count + 7 new).

- [ ] **Step 5: Commit**

```bash
git add services/bettor/pattern.py tests/test_pattern.py
git commit -m "$(cat <<'EOF'
Add a categorical "equals" direction to PatternTracker

Widens threshold/total from int to int | str and adds
direction="equals" (qualifies on total == threshold) alongside the
existing at_or_under/at_or_over numeric comparisons. The fire/skip/reset
life cycle itself is unchanged -- this only generalizes what "qualifies"
means, so Pattern 3's "2X" categorical streak reuses the exact same
state machine Pattern 1/2 already do instead of forking a new one.
Numeric directions are unaffected, confirmed by the existing suite
passing unmodified.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DMfy8WG1Kbn7KX6nf1GywZ
EOF
)"
```

---

## Task 3: Extend `BetExecutor.place_bet()` for the Double Chance market

**Files:**
- Modify: `services/bettor/betting_api.py`
- Modify: `tests/test_betting_api.py`

**Interfaces:**
- Produces: `DOUBLE_CHANCE_GROUP = 8`, `DOUBLE_CHANCE_1X_T = 4`, `DOUBLE_CHANCE_12_T = 5`, `DOUBLE_CHANCE_2X_T = 6` module constants; `BetExecutor.place_bet(match_id, home, away, stake, line: float | None = 6.5, period: Literal[1, 2] = 1, over: bool = True, bet_type: int | None = None, group: int = TOTALS_GROUP) -> BetResult` — the two new parameters both default to Pattern 1/2's existing exact behavior; `bet_type`, when given explicitly, overrides the value `over` would otherwise derive. Consumed by Task 6 (Pattern 3 calls `place_bet(..., line=None, period=1, bet_type=DOUBLE_CHANCE_1X_T, group=DOUBLE_CHANCE_GROUP)`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_betting_api.py`:

```python
def _double_chance_event(coef: float = 4.37, t: int = 4) -> dict:
    return {"T": t, "P": None, "G": 8, "C": coef}


def test_double_chance_bet_type_and_group_target_the_right_market():
    seen_ids = []
    seen_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            seen_ids.append(int(request.url.params["id"]))
            return httpx.Response(200, json=_game_zip_response([_double_chance_event()]))
        body = json.loads(request.content)
        seen_ids.append(body["Events"][0]["GameId"])
        seen_bodies.append(body["Events"][0])
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    from services.bettor.betting_api import DOUBLE_CHANCE_1X_T, DOUBLE_CHANCE_GROUP

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=751444117,
            home="A",
            away="B",
            stake=90,
            line=None,
            period=1,
            bet_type=DOUBLE_CHANCE_1X_T,
            group=DOUBLE_CHANCE_GROUP,
        )
    )
    asyncio.run(executor.aclose())

    assert seen_ids == [751444118, 751444118]  # match_id + FIRST_HALF_ID_OFFSET
    assert seen_bodies[0]["Type"] == 4
    assert seen_bodies[0]["Param"] is None
    assert result.success is True
    assert result.odds == 4.37


def test_double_chance_wrong_group_is_not_matched():
    def handler(request: httpx.Request) -> httpx.Response:
        # a Totals-shaped event with the same Type=4 but the Totals group --
        # must not be mistaken for the Double Chance selection.
        return httpx.Response(200, json=_game_zip_response([{"T": 4, "P": None, "G": 17, "C": 4.37}]))

    from services.bettor.betting_api import DOUBLE_CHANCE_1X_T, DOUBLE_CHANCE_GROUP

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=None,
            bet_type=DOUBLE_CHANCE_1X_T, group=DOUBLE_CHANCE_GROUP,
        )
    )
    asyncio.run(executor.aclose())

    assert result.success is False
    assert result.reason == "market not open (stale-fire guard)"


def test_bet_type_override_ignores_the_over_flag():
    # bet_type, when explicitly given, wins over whatever `over` would
    # otherwise have derived (over's default is True/TOTAL_OVER_T, but
    # Double Chance has no over/under concept at all).
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_double_chance_event(t=6)]))
        body = json.loads(request.content)
        assert body["Events"][0]["Type"] == 6
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    from services.bettor.betting_api import DOUBLE_CHANCE_2X_T, DOUBLE_CHANCE_GROUP

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=None, over=True,
            bet_type=DOUBLE_CHANCE_2X_T, group=DOUBLE_CHANCE_GROUP,
        )
    )
    asyncio.run(executor.aclose())

    assert result.success is True
```

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `.venv/bin/pytest tests/test_betting_api.py -v`
Expected: all 3 new tests fail — `test_double_chance_bet_type_and_group_target_the_right_market` and `test_bet_type_override_ignores_the_over_flag` with `TypeError: place_bet() got an unexpected keyword argument 'bet_type'` (or `ImportError` on the `DOUBLE_CHANCE_*` import), `test_double_chance_wrong_group_is_not_matched` the same way. All pre-existing tests still pass.

- [ ] **Step 3: Implement the extension**

In `services/bettor/betting_api.py`, replace:

```python
FIRST_HALF_ID_OFFSET = 1
SECOND_HALF_ID_OFFSET = 2
TOTALS_GROUP = 17
```

with:

```python
FIRST_HALF_ID_OFFSET = 1
SECOND_HALF_ID_OFFSET = 2
TOTALS_GROUP = 17

# Double Chance -- confirmed live, read-only, against GetGameZip on
# matches in the FC 25. 3x3. Conference League (2860561): Group=8, with
# Type=4/5/6 keyed to 1X/12/2X respectively, Param always null (there's
# no line, unlike Totals). Verified against a live, undecided match
# (0-2 down at the time): 1X priced at 4.37 (unlikely but live), 2X
# priced at 1.001 (near-certain) -- consistent with the actual scoreline.
# Uses the identical match_id/+1/+2 sub-game-id scheme as Totals above.
# See docs/superpowers/specs/2026-09-13-first-half-winner-2x-streak-
# pattern-bettor-design.md's "Market mechanics" section for the full
# investigation, including the one residual risk this doesn't close:
# unlike TOTAL_OVER_T (confirmed via one real captured bet), this
# request shape is inferred by symmetry with Totals, not confirmed via
# an actual placed Double Chance bet.
DOUBLE_CHANCE_GROUP = 8
DOUBLE_CHANCE_1X_T = 4
DOUBLE_CHANCE_12_T = 5
DOUBLE_CHANCE_2X_T = 6
```

Replace the `place_bet` signature and its first three lines:

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

with:

```python
    async def place_bet(
        self,
        match_id: int,
        home: str,
        away: str,
        stake: float,
        line: float | None = 6.5,
        period: Literal[1, 2] = 1,
        over: bool = True,
        bet_type: int | None = None,
        group: int = TOTALS_GROUP,
    ) -> BetResult:
        """`bet_type`/`group` default to Pattern 1/2's Total-market shape,
        derived from `over` exactly as before. Pass both explicitly (as
        Pattern 3 does, for the Double Chance market) to bet a market with
        no over/under concept at all -- in that case `over` is ignored."""
        offset = FIRST_HALF_ID_OFFSET if period == 1 else SECOND_HALF_ID_OFFSET
        game_id = match_id + offset
        if bet_type is None:
            bet_type = TOTAL_OVER_T if over else TOTAL_UNDER_T
```

Replace:

```python
        try:
            coef = await self._current_odds(game_id, line, bet_type)
```

with:

```python
        try:
            coef = await self._current_odds(game_id, line, bet_type, group)
```

Replace the `_current_odds` method:

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

with:

```python
    async def _current_odds(self, game_id: int, line: float | None, bet_type: int, group: int = TOTALS_GROUP) -> float | None:
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
                and event.get("G") == group
            ):
                return event.get("C")
        return None
```

- [ ] **Step 4: Run the tests to verify they all pass**

Run: `.venv/bin/pytest tests/test_betting_api.py -v`
Expected: all tests pass (previous count + 3 new).

- [ ] **Step 5: Commit**

```bash
git add services/bettor/betting_api.py tests/test_betting_api.py
git commit -m "$(cat <<'EOF'
Extend BetExecutor.place_bet for the Double Chance market

Adds bet_type/group parameters (both default to Pattern 1/2's existing
Totals-derived behavior) plus DOUBLE_CHANCE_GROUP/_1X_T/_12_T/_2X_T
constants, reverse-engineered read-only against GetGameZip (Group=8,
Type=4/5/6, Param always null). Widens line to accept None for markets
with no line concept. Pattern 1/2's default-args call shape is
byte-identical after this change. Pattern 3 will call place_bet(...,
line=None, bet_type=DOUBLE_CHANCE_1X_T, group=DOUBLE_CHANCE_GROUP).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DMfy8WG1Kbn7KX6nf1GywZ
EOF
)"
```

---

## Task 4: Widen bettor event schemas for Pattern 3

**Files:**
- Modify: `shared/events.py`
- Modify: `services/display/render.py`

**Interfaces:**
- Produces: `PatternProgress.direction: Literal["at_or_under", "at_or_over", "equals"]`, `.threshold: int | str`, `.total: int | str | None`; `PatternArmed.qualifying_totals: list[int | str]`; `BetPlaced.line: float | None = None` (was required `float`). No new event *kinds*. Consumed by Task 6.

- [ ] **Step 1: Update the event models in `shared/events.py`**

Replace:

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
```

with:

```python
class PatternArmed(BaseModel):
    kind: Literal["pattern_armed"] = "pattern_armed"
    pattern_name: str
    qualifying_totals: list[int | str]
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
    direction: Literal["at_or_under", "at_or_over", "equals"]
    streak: int
    streak_length: int
    threshold: int | str
    total: int | str | None
    outcome: Literal["qualifying", "reset", "skipped"]


class BetPlaced(BaseModel):
    kind: Literal["bet_placed"] = "bet_placed"
    match_id: int
    league_name: str
    home: str
    away: str
    stake: float
    line: float | None = None  # None for a line-less market (e.g. Pattern 3's Double Chance)
    market_label: str
    odds: float | None = None
```

- [ ] **Step 2: Sanity-check the models round-trip through JSON with the widened fields**

Run:
```bash
.venv/bin/python -c "
from shared.events import BetPlaced, PatternArmed, PatternProgress
b = BetPlaced(match_id=1, league_name='L', home='A', away='B', stake=90, line=None, market_label='Double Chance. 1st half 1X')
print(BetPlaced.model_validate_json(b.model_dump_json()))
a = PatternArmed(pattern_name='x', qualifying_totals=['2X', '2X'], market_label='m', condition_label='c')
print(PatternArmed.model_validate_json(a.model_dump_json()))
p = PatternProgress(match_id=1, pattern_name='x', direction='equals', streak=1, streak_length=2, threshold='2X', total='2X', outcome='qualifying')
print(PatternProgress.model_validate_json(p.model_dump_json()))
"
```
Expected: all three reconstructed objects print, no exception.

- [ ] **Step 3: Add the `"equals"` case to `render_pattern_progress`**

In `services/display/render.py`, replace:

```python
def render_pattern_progress(event: PatternProgress) -> None:
    """One dim line narrating what the just-finished round did to the
    streak — printed for every round (see render_pattern_armed for the
    moment it actually fires, which this deliberately doesn't duplicate).
    `direction` picks the qualify/reset comparison wording so this reads
    correctly for both Pattern 1 (at_or_under) and Pattern 2 (at_or_over)."""
    qualify_cmp = "≤" if event.direction == "at_or_under" else "≥"
    reset_cmp = ">" if event.direction == "at_or_under" else "<"
```

with:

```python
def render_pattern_progress(event: PatternProgress) -> None:
    """One dim line narrating what the just-finished round did to the
    streak — printed for every round (see render_pattern_armed for the
    moment it actually fires, which this deliberately doesn't duplicate).
    `direction` picks the qualify/reset comparison wording so this reads
    correctly for Pattern 1 (at_or_under), Pattern 2 (at_or_over), and
    Pattern 3 (equals, a categorical match rather than a numeric one)."""
    if event.direction == "equals":
        qualify_cmp, reset_cmp = "==", "!="
    elif event.direction == "at_or_under":
        qualify_cmp, reset_cmp = "≤", ">"
    else:
        qualify_cmp, reset_cmp = "≥", "<"
```

The rest of the function (the `this_round`/`detail`/`console.print` lines below) is unchanged — its `f"{this_round} {cmp} {event.threshold}"` templating already works correctly for string values.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass, unchanged in count from Task 3 (this task adds no new test file — `render.py` has no existing unit test suite to extend, consistent with the rest of this module; Task 6's wiring is what actually exercises `direction="equals"` end-to-end).

Run: `.venv/bin/python -c "import services.display.render; import shared.events"`
Expected: no exception.

- [ ] **Step 5: Commit**

```bash
git add shared/events.py services/display/render.py
git commit -m "$(cat <<'EOF'
Widen bettor event schemas for Pattern 3's categorical streak

PatternProgress.direction gains "equals"; .threshold/.total and
PatternArmed.qualifying_totals widen from int to int | str so Pattern 3
can publish its "2X"/"1X" results instead of an opaque numeric encoding.
BetPlaced.line becomes optional (None) for a line-less market like
Double Chance -- it was already unused by every render/log consumer, so
this is a pure widening, not a behavior change for Pattern 1/2, which
keep passing a concrete line as before. render_pattern_progress gains
the "==" / "!=" wording for the new direction. No new event kinds.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DMfy8WG1Kbn7KX6nf1GywZ
EOF
)"
```

---

## Task 5: New `PATTERN3_*` config knobs

**Files:**
- Modify: `shared/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `Config.pattern3_streak_length: int`, `Config.pattern3_bet_stake_amount: float`, `Config.pattern3_enabled: bool` — consumed by Task 6.

- [ ] **Step 1: Add the new fields to the `Config` dataclass**

In `shared/config.py`, replace:

```python
    pattern2_bet_stake_amount: float
    pattern2_enabled: bool
    cdp_url: str
```

with:

```python
    pattern2_bet_stake_amount: float
    pattern2_enabled: bool
    pattern3_streak_length: int
    pattern3_bet_stake_amount: float
    pattern3_enabled: bool
    cdp_url: str
```

- [ ] **Step 2: Populate them in `load_config()`**

Replace:

```python
        pattern2_bet_stake_amount=float(os.environ.get("PATTERN2_BET_STAKE_AMOUNT", "90")),
        pattern2_enabled=_bool("PATTERN2_ENABLED", True),
        cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
```

with:

```python
        pattern2_bet_stake_amount=float(os.environ.get("PATTERN2_BET_STAKE_AMOUNT", "90")),
        pattern2_enabled=_bool("PATTERN2_ENABLED", True),
        pattern3_streak_length=int(os.environ.get("PATTERN3_STREAK_LENGTH", "2")),
        pattern3_bet_stake_amount=float(os.environ.get("PATTERN3_BET_STAKE_AMOUNT", "90")),
        pattern3_enabled=_bool("PATTERN3_ENABLED", True),
        cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
```

- [ ] **Step 3: Verify defaults load cleanly**

Run: `.venv/bin/python -c "from shared.config import load_config; c = load_config(); print(c.pattern3_streak_length, c.pattern3_bet_stake_amount, c.pattern3_enabled)"`
Expected: `2 90.0 True`

- [ ] **Step 4: Document the new variables in `.env.example`**

Append to `.env.example`, right after the existing Pattern 2 section:

```bash
# --- Betting: Pattern 3 — "1st Half Winner 2X" streak (services/bettor) ---
# Fires after PATTERN3_STREAK_LENGTH consecutive finished rounds each have
# a 1st-half Double Chance result of exactly "2X" (away win or draw);
# bets on the next round's Double Chance. 1st half market, selection 1X.
# Real money, live from the first run if PATTERN3_ENABLED is true -- see
# README.md "Betting patterns" before changing these. Runs as a third,
# independent pattern in the same services/bettor process as Pattern 1/2
# -- no separate service to start.
PATTERN3_STREAK_LENGTH=2
PATTERN3_BET_STAKE_AMOUNT=90
# Kill switch for Pattern 3 only -- Pattern 1/2 are unaffected either way.
# Defaults to true (Pattern 3 goes live immediately) if unset. Set to
# false to pause Pattern 3: it keeps tracking the streak and
# logging/publishing PatternProgress as normal, but never arms a target
# or calls BetExecutor, so it cannot place a bet while off.
PATTERN3_ENABLED=true
```

- [ ] **Step 5: Commit**

```bash
git add shared/config.py .env.example
git commit -m "$(cat <<'EOF'
Add config knobs for the 1st-half-winner-2X streak betting pattern

PATTERN3_STREAK_LENGTH, PATTERN3_BET_STAKE_AMOUNT, PATTERN3_ENABLED --
same load_config()/.env convention as Pattern 1/2's own knobs, fully
independent of them. PATTERN3_ENABLED defaults true per explicit prior
decision -- Pattern 3 goes live immediately once wired into main.py.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DMfy8WG1Kbn7KX6nf1GywZ
EOF
)"
```

---

## Task 6: Wire Pattern 3 into `services/bettor/main.py`

**This is the one task that touches already-shipped, currently-live
wiring for Pattern 1 and Pattern 2** — `place()`'s signature changes from
a Total-market shape (`period`/`over`/`line`) to a market-agnostic one
(`market_label`/`place_call`), and its mutual-exclusion check changes
from one other tracker to a list. Every existing Pattern 1/2 call site is
updated in this same task so the tree stays consistent, and the full
existing test suite must pass afterward as proof this is behavior-
preserving for them.

**Files:**
- Modify: `services/bettor/main.py`

**Interfaces:**
- Consumes: `PatternTracker(direction="equals", threshold="2X", ...)` (Task 2), `BetExecutor.place_bet(bet_type=..., group=...)` + `DOUBLE_CHANCE_1X_T`/`DOUBLE_CHANCE_GROUP` (Task 3), widened events (Task 4), `config.pattern3_*` (Task 5), `double_chance_winner()` (Task 1).
- Produces: the full three-pattern `consume()` loop — `python -m services.bettor.main` now runs all three patterns.

- [ ] **Step 1: Update imports and add `PATTERN3_NAME` + two new label helpers**

Replace:

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

PATTERN1_NAME = "1st_half_over_6.5_streak"
PATTERN2_NAME = "2nd_half_under_7.5_streak"


def _market_label(period: int, over: bool, line: float) -> str:
    half = "1st half" if period == 1 else "2nd half"
    side = "Over" if over else "Under"
    return f"Total. {half} {side} {line:g}"


def _condition_label(direction: str, period: int, threshold: int, streak_length: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    cmp = "<=" if direction == "at_or_under" else ">="
    return f"{streak_length} consecutive rounds with {half} total {cmp} {threshold}"
```

with:

```python
from typing import Awaitable, Callable

from services.bettor.betting_api import (
    BetExecutor,
    BetResult,
    DOUBLE_CHANCE_1X_T,
    DOUBLE_CHANCE_GROUP,
)
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
from shared.markets import double_chance_winner

PATTERN1_NAME = "1st_half_over_6.5_streak"
PATTERN2_NAME = "2nd_half_under_7.5_streak"
PATTERN3_NAME = "1st_half_winner_2x_streak"


def _market_label(period: int, over: bool, line: float) -> str:
    half = "1st half" if period == 1 else "2nd half"
    side = "Over" if over else "Under"
    return f"Total. {half} {side} {line:g}"


def _condition_label(direction: str, period: int, threshold: int, streak_length: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    cmp = "<=" if direction == "at_or_under" else ">="
    return f"{streak_length} consecutive rounds with {half} total {cmp} {threshold}"


def _double_chance_market_label(period: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    return f"Double Chance. {half} 1X"


def _winner_condition_label(streak_length: int) -> str:
    return f"{streak_length} consecutive rounds with 1st half winner 2X"
```

(`Awaitable`/`Callable`/`BetResult` are needed for `place()`'s new `place_call` parameter's type hint in Step 3.)

- [ ] **Step 2: Construct Pattern 3's tracker and target-tracker instances**

Replace:

```python
    tracker2 = PatternTracker(
        threshold=config.pattern2_high_threshold,
        streak_length=config.pattern2_streak_length,
        direction="at_or_over",
    )
    targets2 = TargetTracker(stale_statuses={"finished"})
    placed_matches2: set[int] = set()

    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting Pattern 1 — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )
    log.info(
        f"starting Pattern 2 — streak_length={config.pattern2_streak_length} "
        f"high_threshold={config.pattern2_high_threshold} bet_line={config.pattern2_bet_line} "
        f"stake={config.pattern2_bet_stake_amount} "
        f"{'ENABLED' if config.pattern2_enabled else 'DISABLED (PATTERN2_ENABLED=false) — tracking only, will not bet'}"
    )
```

with:

```python
    tracker2 = PatternTracker(
        threshold=config.pattern2_high_threshold,
        streak_length=config.pattern2_streak_length,
        direction="at_or_over",
    )
    targets2 = TargetTracker(stale_statuses={"finished"})
    placed_matches2: set[int] = set()

    tracker3 = PatternTracker(
        threshold="2X",
        streak_length=config.pattern3_streak_length,
        direction="equals",
    )
    targets3 = TargetTracker()
    placed_matches3: set[int] = set()

    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting Pattern 1 — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )
    log.info(
        f"starting Pattern 2 — streak_length={config.pattern2_streak_length} "
        f"high_threshold={config.pattern2_high_threshold} bet_line={config.pattern2_bet_line} "
        f"stake={config.pattern2_bet_stake_amount} "
        f"{'ENABLED' if config.pattern2_enabled else 'DISABLED (PATTERN2_ENABLED=false) — tracking only, will not bet'}"
    )
    log.info(
        f"starting Pattern 3 — streak_length={config.pattern3_streak_length} "
        f"trigger=2X selection=1X stake={config.pattern3_bet_stake_amount} "
        f"{'ENABLED' if config.pattern3_enabled else 'DISABLED (PATTERN3_ENABLED=false) — tracking only, will not bet'}"
    )
```

- [ ] **Step 3: Replace `place()` with a market-agnostic version shared by all three patterns**

Replace the entire `place()` function with:

```python
    async def place(
        target: MatchDiscovered,
        *,
        targets: TargetTracker,
        other_patterns: list[tuple[TargetTracker, str]],
        placed_matches: set[int],
        market_label: str,
        stale_period_label: str,
        line: float | None,
        stake: float,
        place_call: Callable[[], Awaitable[BetResult]],
    ) -> None:
        for other_targets, other_pattern_name in other_patterns:
            conflict = mutual_exclusion_reason(target.match_id, other_pattern_name, other_targets.bet_targets)
            if conflict is not None:
                log.warning(conflict)
                await bus.publish(
                    config.channel_match_events,
                    BetFailed(match_id=target.match_id, reason=conflict, market_label=market_label),
                )
                return

        if targets.is_stale(target.match_id):
            reason = (
                f"stale: match {target.match_id} ({target.home} vs {target.away}) "
                f"already past its {stale_period_label} by the time the bet was attempted"
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
        result = await place_call()
        if result.success:
            log.info(f"bet placed on match {target.match_id} at odds {result.odds}")
            placed_matches.add(target.match_id)
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

Note what changed and why: `period`/`over` are gone (they only ever existed to build `market_label` and drive `executor.place_bet()`, both of which the caller now does itself via `market_label`/`place_call`); `stale_period_label` replaces the inline `"1st half" if period == 1 else "2nd half"` derivation so the stale-fire `BetFailed` reason text is byte-identical to today for Pattern 1/2; `other_targets`/`other_pattern_name` become `other_patterns: list[tuple[...]]` so three (not two) patterns can mutually exclude each other; `line` is now a caller-supplied value (not derived) since `place()` no longer knows what market it's placing.

- [ ] **Step 4: Update Pattern 1 and Pattern 2's existing call sites to the new `place()` signature, and add Pattern 3's wiring**

Replace the entire `consume()` function with:

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
                            other_patterns=[(targets2, PATTERN2_NAME), (targets3, PATTERN3_NAME)],
                            placed_matches=placed_matches,
                            market_label=_market_label(1, True, config.pattern_bet_line),
                            stale_period_label="1st half",
                            line=config.pattern_bet_line,
                            stake=config.bet_stake_amount,
                            place_call=lambda: executor.place_bet(
                                match_id=target.match_id,
                                home=target.home,
                                away=target.away,
                                stake=config.bet_stake_amount,
                                line=config.pattern_bet_line,
                                period=1,
                                over=True,
                            ),
                        )
                    target2 = targets2.on_discovered(event)
                    if target2 is not None and config.pattern2_enabled:
                        await place(
                            target2,
                            targets=targets2,
                            other_patterns=[(targets, PATTERN1_NAME), (targets3, PATTERN3_NAME)],
                            placed_matches=placed_matches2,
                            market_label=_market_label(2, False, config.pattern2_bet_line),
                            stale_period_label="2nd half",
                            line=config.pattern2_bet_line,
                            stake=config.pattern2_bet_stake_amount,
                            place_call=lambda: executor.place_bet(
                                match_id=target2.match_id,
                                home=target2.home,
                                away=target2.away,
                                stake=config.pattern2_bet_stake_amount,
                                line=config.pattern2_bet_line,
                                period=2,
                                over=False,
                            ),
                        )
                    target3 = targets3.on_discovered(event)
                    if target3 is not None and config.pattern3_enabled:
                        await place(
                            target3,
                            targets=targets3,
                            other_patterns=[(targets, PATTERN1_NAME), (targets2, PATTERN2_NAME)],
                            placed_matches=placed_matches3,
                            market_label=_double_chance_market_label(1),
                            stale_period_label="1st half",
                            line=None,
                            stake=config.pattern3_bet_stake_amount,
                            place_call=lambda: executor.place_bet(
                                match_id=target3.match_id,
                                home=target3.home,
                                away=target3.away,
                                stake=config.pattern3_bet_stake_amount,
                                line=None,
                                period=1,
                                bet_type=DOUBLE_CHANCE_1X_T,
                                group=DOUBLE_CHANCE_GROUP,
                            ),
                        )
                elif isinstance(event, MatchStarted):
                    targets.on_started(event.match_id)
                    targets2.on_started(event.match_id)
                    targets3.on_started(event.match_id)
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                    targets2.on_half_time(event.match_id)
                    targets3.on_half_time(event.match_id)

                    first_half_total = event.first_half.home_goals + event.first_half.away_goals
                    winner_1h = double_chance_winner(event.first_half.home_goals, event.first_half.away_goals)

                    if event.match_id in placed_matches:
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

                    if event.match_id in placed_matches3:
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=(winner_1h == "1X"),
                                period_total=first_half_total,
                                market_label=_double_chance_market_label(1),
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
                                other_patterns=[(targets2, PATTERN2_NAME), (targets3, PATTERN3_NAME)],
                                placed_matches=placed_matches,
                                market_label=_market_label(1, True, config.pattern_bet_line),
                                stale_period_label="1st half",
                                line=config.pattern_bet_line,
                                stake=config.bet_stake_amount,
                                place_call=lambda: executor.place_bet(
                                    match_id=target.match_id,
                                    home=target.home,
                                    away=target.away,
                                    stake=config.bet_stake_amount,
                                    line=config.pattern_bet_line,
                                    period=1,
                                    over=True,
                                ),
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

                    fired3 = tracker3.process(winner_1h)
                    if fired3:
                        if not config.pattern3_enabled:
                            log.warning(
                                f"PATTERN 3 ARMED — streak {tracker3.last_streak_totals} "
                                "— but PATTERN3_ENABLED=false, suppressing bet placement"
                            )
                        else:
                            log.info(f"PATTERN 3 ARMED — streak {tracker3.last_streak_totals}")
                            await bus.publish(
                                config.channel_match_events,
                                PatternArmed(
                                    pattern_name=PATTERN3_NAME,
                                    qualifying_totals=list(tracker3.last_streak_totals),
                                    market_label=_double_chance_market_label(1),
                                    condition_label=_winner_condition_label(config.pattern3_streak_length),
                                ),
                            )
                            target3 = targets3.arm()
                            if target3 is not None:
                                await place(
                                    target3,
                                    targets=targets3,
                                    other_patterns=[(targets, PATTERN1_NAME), (targets2, PATTERN2_NAME)],
                                    placed_matches=placed_matches3,
                                    market_label=_double_chance_market_label(1),
                                    stale_period_label="1st half",
                                    line=None,
                                    stake=config.pattern3_bet_stake_amount,
                                    place_call=lambda: executor.place_bet(
                                        match_id=target3.match_id,
                                        home=target3.home,
                                        away=target3.away,
                                        stake=config.pattern3_bet_stake_amount,
                                        line=None,
                                        period=1,
                                        bet_type=DOUBLE_CHANCE_1X_T,
                                        group=DOUBLE_CHANCE_GROUP,
                                    ),
                                )
                    else:
                        await bus.publish(
                            config.channel_match_events,
                            PatternProgress(
                                match_id=event.match_id,
                                pattern_name=PATTERN3_NAME,
                                direction="equals",
                                streak=tracker3.streak,
                                streak_length=config.pattern3_streak_length,
                                threshold="2X",
                                total=tracker3.last_total,
                                outcome=tracker3.last_outcome,
                            ),
                        )
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)
                    targets2.on_finished(event.match_id)
                    targets3.on_finished(event.match_id)

                    second_half_total = (
                        None if event.second_half is None
                        else event.second_half.home_goals + event.second_half.away_goals
                    )

                    if event.match_id in placed_matches2 and second_half_total is not None:
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
                        if not config.pattern2_enabled:
                            log.warning(
                                f"PATTERN 2 ARMED — streak {tracker2.last_streak_totals} "
                                "— but PATTERN2_ENABLED=false, suppressing bet placement"
                            )
                        else:
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
                                    other_patterns=[(targets, PATTERN1_NAME), (targets3, PATTERN3_NAME)],
                                    placed_matches=placed_matches2,
                                    market_label=_market_label(2, False, config.pattern2_bet_line),
                                    stale_period_label="2nd half",
                                    line=config.pattern2_bet_line,
                                    stake=config.pattern2_bet_stake_amount,
                                    place_call=lambda: executor.place_bet(
                                        match_id=target2.match_id,
                                        home=target2.home,
                                        away=target2.away,
                                        stake=config.pattern2_bet_stake_amount,
                                        line=config.pattern2_bet_line,
                                        period=2,
                                        over=False,
                                    ),
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

- [ ] **Step 5: Smoke-test the module imports and constructs cleanly**

Run: `.venv/bin/python -c "import services.bettor.main"`
Expected: no exception.

- [ ] **Step 6: Run the full existing test suite to confirm Pattern 1/2 are unaffected**

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass (no count change from Task 5 — this task adds no new test file, since `main.py`'s `consume()` loop has never had direct unit test coverage in this repo; Pattern 1/2's unchanged behavior is proven by their existing `PatternTracker`/`BetExecutor`/`TargetTracker` tests still passing against the refactored call sites, and by this smoke import).

- [ ] **Step 7: Manual end-to-end sanity check against the live event stream**

With Redis and the collector/aggregator running (`./run.sh status` — see the repo's own runbook), start the bettor and confirm all three patterns log their startup lines:

Run: `.venv/bin/python -m services.bettor.main` (foreground, Ctrl-C to stop after confirming)
Expected: three `starting Pattern N — ...` log lines (Pattern 3's ending in `ENABLED`), then normal `PatternProgress`-driven activity as live rounds finish — no exceptions, no `failed to process ...` lines.

- [ ] **Step 8: Commit**

```bash
git add services/bettor/main.py
git commit -m "$(cat <<'EOF'
Wire Pattern 3 into services/bettor/main.py

Generalizes place() from a Total-market shape (period/over/line) to a
market-agnostic one (market_label/place_call), and its mutual-exclusion
check from one other tracker to a list of them -- both needed so
Pattern 3's Double Chance bet can share the same helper Pattern 1/2 use,
and so all three patterns exclude each other. Pattern 1/2's call sites
are updated to the new signature with no behavior change (proven by the
existing suite passing unmodified). Adds Pattern 3's own tracker/target-
tracker/placed-matches state, its MatchHalfTime evaluation (same trigger
event as Pattern 1) alongside Pattern 1's, and its MatchDiscovered/
MatchStarted/MatchFinished hooks.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DMfy8WG1Kbn7KX6nf1GywZ
EOF
)"
```

---

## Task 7: Document Pattern 3 in `README.md`

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing new (documentation only).

- [ ] **Step 1: Add a "Pattern 3" subsection to the "Betting patterns" section**

In `README.md`, immediately after the existing Pattern 2 subsection (right before the `## How data is sourced` heading), insert:

```markdown
### Pattern 3 — "1st Half Winner 2X" streak

A categorical mean-reversion pattern, run as a third, independent pattern
in the same `services/bettor/` process as Pattern 1/2 — no separate
service, no separate CDP session. It watches every finished round's
**1st-half Double Chance result** — "1X" (home win or draw), "2X" (away
win or draw), or "X" (outright draw) — instead of a numeric goal total.
`PATTERN3_STREAK_LENGTH` (default 2) consecutive rounds each settling
exactly "2X" fire a real bet — `PATTERN3_BET_STAKE_AMOUNT` (default 90,
FCFA, independently configurable from Pattern 1/2's stakes) on the *next*
round's `Double Chance. 1st half` market, selection `1X`. Same fire → skip
one round → restart life cycle as Pattern 1/2, just qualifying on category
equality (`services/bettor/pattern.py`'s `PatternTracker(direction="equals")`)
rather than a numeric comparison.

Like Pattern 1, Pattern 3 evaluates at each round's `MatchHalfTime` event
— the 1st-half result is fully known there, no need to wait for
`MatchFinished`.

The Double Chance market (`Group=8` on the 1xbet JSON API, `Type=4/5/6`
for 1X/12/2X) was reverse-engineered read-only against live odds, the
same way the original Total market was — see
`services/bettor/betting_api.py`'s module docstring. Unlike the Total
market, this was **not** confirmed via an actual placed bet before
shipping; watch the first live Pattern 3 fire closely.

**Mutual exclusion** now spans all three patterns: no two of them ever
place a bet on the same match in the same round. This caps risk *per
match*, not in aggregate — running all three roughly triples the
aggregate stake-rate exposure compared to Pattern 1 running alone.

Config knobs: `PATTERN3_STREAK_LENGTH`, `PATTERN3_BET_STAKE_AMOUNT`,
`PATTERN3_ENABLED` — see `.env.example`. Shares `CDP_URL` and
`BETS_LOG_PATH` with Pattern 1/2.
```

- [ ] **Step 2: Update the mutual-exclusion sentence in the Pattern 2 subsection**

Pattern 2's own subsection currently describes mutual exclusion as
two-way. Replace:

```markdown
**Mutual exclusion:** since both patterns watch the same "next match to
kick off," they can resolve to targeting the same match in the same
round. Only one bet per match is ever placed — whichever pattern's
target resolves first wins it; the other logs a `BET FAILED` with a
`mutual exclusion: ...` reason instead of also staking money on it.
This caps risk *per match*, not in aggregate: the two patterns can each
have an independent stake open on a *different* match at the same time,
so running both roughly doubles the aggregate stake-rate exposure
compared to Pattern 1 running alone.
```

with:

```markdown
**Mutual exclusion:** since all three patterns watch the same "next match
to kick off," they can resolve to targeting the same match in the same
round. Only one bet per match is ever placed — whichever pattern's
target resolves first wins it; the others log a `BET FAILED` with a
`mutual exclusion: ...` reason instead of also staking money on it. See
Pattern 3's subsection below for the full three-way picture.
```

- [ ] **Step 3: Add the new config knobs to the "Configuration reference" table**

The table currently has rows for Pattern 1's knobs only (Pattern 2's are
documented only in its own prose subsection, a pre-existing gap this task
doesn't need to close). Replace:

```markdown
| `PATTERN_BET_LINE` | `6.5` | The Over line bet on in `Total. 1st half`. |
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the already-logged-in browser the bettor reads fresh auth from (read-only touch, not UI automation). |
```

with:

```markdown
| `PATTERN_BET_LINE` | `6.5` | The Over line bet on in `Total. 1st half`. |
| `PATTERN3_STREAK_LENGTH` | `2` | Consecutive rounds required, each with a 1st-half Double Chance result of exactly "2X", to fire Pattern 3. |
| `PATTERN3_BET_STAKE_AMOUNT` | `90` | FCFA staked per fired Pattern 3 bet. |
| `PATTERN3_ENABLED` | `true` | Kill switch for Pattern 3 only — set `false` to track without betting. |
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the already-logged-in browser the bettor reads fresh auth from (read-only touch, not UI automation). |
```

- [ ] **Step 4: Update the roadmap paragraph**

In `README.md`'s `## Roadmap` section, replace:

```markdown
Extraction + real-time terminal display, plus two live betting patterns
(see [Betting patterns](#betting-patterns)), are all implemented. Both
patterns run in the same `services/bettor/` process, coordinated so they
never both bet on the same match in the same round. Any future pattern
follows the same shape: extend `PatternTracker`/`TargetTracker`/
`BetExecutor` rather than forking them, and publish its own
`PatternArmed`/`Bet*` events onto the same `xbet.match_events` bus.
```

with:

```markdown
Extraction + real-time terminal display, plus three live betting patterns
(see [Betting patterns](#betting-patterns)), are all implemented. All
three patterns run in the same `services/bettor/` process, coordinated so
no two of them ever bet on the same match in the same round. Any future
pattern follows the same shape: extend `PatternTracker`/`TargetTracker`/
`BetExecutor` rather than forking them, and publish its own
`PatternArmed`/`Bet*` events onto the same `xbet.match_events` bus.
```

- [ ] **Step 5: Proofread**

Run: `grep -n "Pattern 3" README.md` and confirm every new reference reads correctly in context (open the file and skim each match).

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document Pattern 3 in README

Adds the "1st Half Winner 2X" streak subsection, updates the Pattern 2
mutual-exclusion note to point at the new three-way picture, adds the
PATTERN3_* rows to the configuration reference table, and updates the
roadmap's pattern count.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DMfy8WG1Kbn7KX6nf1GywZ
EOF
)"
```

---

## Task 8: Final full-suite regression + first-live-fire watch checklist

Pattern 3 ships with `PATTERN3_ENABLED=true` (explicit prior decision) —
there is no authorization gate to hold open here, unlike a pattern that
ships disabled-by-default. This task's job is to confirm the whole
three-pattern system is actually consistent end to end, and to leave a
concrete checklist for watching the very first real Pattern 3 bet, since
its request shape is inferred (see Task 3's docstring addition) rather
than confirmed via a prior captured bet the way Pattern 1's was.

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite one more time from a clean state**

Run: `.venv/bin/pytest tests/ -v`
Expected: every test across `test_pattern.py`, `test_betting_api.py`,
`test_targeting.py`, and the new `test_markets.py` passes. Note the final
total in the task's completion notes for the record.

- [ ] **Step 2: Confirm `git log` shows a clean, complete task sequence**

Run: `git log --oneline -10`
Expected: one commit per task above (8 total, this task's excepted since
it makes no file changes), each with a clear, single-purpose message.

- [ ] **Step 3: Restart the bettor process and confirm all three patterns start cleanly**

Follow this repo's own runbook (`./run.sh status`, `tail logs/bettor.log`
— see the top-level `CLAUDE.md` for this feature) to restart
`services/bettor/main.py` onto the new three-pattern code. Confirm the
log shows all three `starting Pattern N — ...` lines (Pattern 3's ending
in `ENABLED`), and that `PatternProgress` events for all three pattern
names appear as live rounds finish, with no `failed to process ...`
error lines.

- [ ] **Step 4: Leave a first-live-fire watch checklist for whoever is present when Pattern 3 first arms**

Because Pattern 3's bet-placement shape was reverse-engineered read-only
(see Task 3), the first real fire deserves the same scrutiny Pattern 1's
original live trial got. When `PATTERN 3 ARMED` first appears in
`logs/bettor.log` / `data/bets.log`:

- Confirm the subsequent `BET PLACED` (not `BET FAILED`) line shows a
  plausible odds value for a Double Chance 1X selection (typically
  between roughly 1.0 and 3.0 for a competitive match — a wildly
  different number, e.g. >10 or <1.01 while the match looks competitive,
  is a signal something matched the wrong market row).
- If it instead fails, read the `reason` string carefully: `"market not
  open (stale-fire guard)"` means the odds lookup found no `Group=8`/
  `Type=4`/`Param=null` row for that sub-game id at that moment (network
  timing, not necessarily a wrong-market bug — Pattern 1 sees the same
  failure mode occasionally). A non-2xx/`Success: false` response with an
  `Error` string from the site itself is the more informative signal to
  investigate if it recurs.
- Once the match reaches half-time, confirm the `SETTLED` line's `WON`/
  `LOST` call agrees with the actual 1st-half result shown in the
  `RESULT`/live-score panel for that round (i.e. `won=True` iff the 1st
  half was a home win or draw) — this is a pure arithmetic check against
  `shared/markets.py`'s `double_chance_winner()`, already unit-tested in
  Task 1, but confirming it against one real live round closes the loop.

No code changes are expected from this step — it's a verification
checklist, not a gate that gets checked off by editing anything. If a
real discrepancy turns up, treat it as a new bug to fix (systematic-
debugging skill, not a patch bolted directly onto this plan).
