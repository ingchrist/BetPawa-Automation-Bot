# Pattern 4 ("Main Game Under 16.5" pair-streak) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fourth, independent automated betting pattern to the already-live `services/bettor/` process — detect a 2-round pair where at least 3 of the 4 (1st-half-total, 2nd-half-total) values are ≥ 9, then place a real bet on the next round's `Total. Main game` market, `Under 16.5`, waiting for the live odds to clear 1.5 before submitting.

**Architecture:** No new service. A new `RoundPairStreakTracker` (in `services/bettor/pattern.py`, alongside the existing `PatternTracker`) encodes the pair-counting rule as its own small pure state machine — it doesn't fit `PatternTracker`'s single-scalar-streak shape as a parameterization. `BetExecutor.place_bet()` (`services/bettor/betting_api.py`) gets two additive extensions: a `period=0` "main game" option, and an opt-in `min_odds`/`poll_interval_seconds`/`is_stale` wait loop — both default-preserve Patterns 1–3's exact existing behavior. `services/bettor/main.py` wires a fourth handler set (own tracker, own `TargetTracker`, own `placed_matches` set) into the existing `consume()` loop, with one structural difference from Patterns 1–3: because Pattern 4's `place_bet()` can now block for however long a match takes, its `place(...)` calls run as `asyncio.create_task(...)` rather than being `await`ed inline, so they never stall the other three patterns' event processing. `shared/events.py`'s `PatternProgress` gains two additive literal values (`"pair_count"` direction, `"counting"` outcome) so Pattern 4's per-round narration doesn't misrepresent its shape; `services/display/render.py` gets one new early-return branch for that direction.

**Tech Stack:** Python 3.10, asyncio, pydantic, Redis pub/sub (existing `shared/bus.py`), `rich` (existing `services/display`), `httpx` (existing `services/bettor/betting_api.py`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-16-main-game-under-16.5-pair-streak-pattern-design.md`

## Global Constraints

- Pattern 4 fires when ≥ 3 of the 4 values (1st-half-total, 2nd-half-total, from each of 2 consecutive finished rounds) are ≥ 9 (`half_threshold=9`, `required_count=3` — hardcoded, not env-configurable). Bets on the *next* round's `Total. Main game` market, `Under PATTERN4_BET_LINE` (default `16.5`).
- Non-overlapping pairs: after 2 rounds are evaluated (fire or not), the pair buffer clears and a fresh pair starts from the next round. After a fire, the round immediately following is additionally skipped entirely (not counted toward the next pair) before counting resumes — same fire → skip-one-round → restart shape `PatternTracker` already has, applied to a pair-based counter instead of a single-scalar streak.
- Evaluates at `MatchFinished` (needs both halves of a round), not `MatchHalfTime`.
- Stake is `PATTERN4_BET_STAKE_AMOUNT`, default `90` (FCFA) — independently configurable, never hardcoded at the call site.
- New `BetExecutor` behavior, exclusive to Pattern 4: before submitting, wait for the market's live odds to reach `PATTERN4_MIN_ODDS = 1.5` (a hardcoded module constant, not env-configurable), polling every `PATTERN4_ODDS_POLL_INTERVAL_SECONDS = 5.0` seconds. This wait has **no timeout of its own** — it only stops when the odds clear 1.5 (bet placed at that price) or the target match reaches `"finished"` status (bet abandoned, logged, no exception raised).
- No mutual exclusion between Pattern 4 and Patterns 1–3, in either direction — Pattern 4 targets a market none of them touch, so it neither blocks nor is blocked by them.
- Pattern 4's `place(...)` calls run as `asyncio.create_task(...)`, never `await`ed inline in `consume()` — the one structural deviation from Patterns 1–3, required because its wait can span an entire match.
- `PATTERN4_ENABLED` defaults to `true` — ships live from the first run, same as Pattern 1 and Pattern 3.
- Every existing test in `tests/` (`test_pattern.py`, `test_betting_api.py`, `test_targeting.py`, `test_markets.py`, `test_session_watchdog.py`) must still pass, unmodified in behavior, after every task in this plan — Patterns 1–3 are live, real-money software; nothing here may change their behavior.

---

## Task 1: `RoundPairStreakTracker` — the pair-counting state machine

**Files:**
- Modify: `services/bettor/pattern.py`
- Modify: `tests/test_pattern.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `RoundPairStreakTracker(half_threshold: int = 9, required_count: int = 3)` with `.process(first_half_total: int | None, second_half_total: int | None) -> bool`, `.rounds_in_pair` (property, 0 or 1), `.last_pair_values: list[int]`, `.last_qualifying_count: int`, `.last_outcome: Literal["counting", "reset", "skipped", "armed"] | None`. Consumed by Task 5 (`main.py`'s `tracker4`).

- [ ] **Step 1: Write the failing tests**

Update the import line at the top of `tests/test_pattern.py`:

```python
from services.bettor.pattern import PatternTracker, RoundPairStreakTracker
```

Append to the end of `tests/test_pattern.py`:

```python
def test_pair_fires_when_three_of_four_values_qualify():
    # Reference example: Round 1 (1st-half=10, 2nd-half=8), Round 2
    # (1st-half=10, 2nd-half=12) -> values [10, 8, 10, 12] -> 3 of 4 >= 9.
    tracker = RoundPairStreakTracker()
    assert tracker.process(10, 8) is False
    assert tracker.process(10, 12) is True
    assert tracker.last_pair_values == [10, 8, 10, 12]


def test_pair_one_round_entirely_under_threshold_prevents_fire():
    tracker = RoundPairStreakTracker()
    assert tracker.process(5, 5) is False  # 0 of 2 qualify
    assert tracker.process(10, 12) is False  # values [5,5,10,12] -- only 2 of 4 qualify


def test_pair_fires_when_exactly_one_value_is_under_threshold():
    tracker = RoundPairStreakTracker()
    assert tracker.process(9, 9) is False
    assert tracker.process(9, 8) is True  # values [9,9,9,8] -- 3 of 4 qualify
    assert tracker.last_pair_values == [9, 9, 9, 8]


def test_pair_fire_then_skip_one_round_then_restart():
    tracker = RoundPairStreakTracker()
    tracker.process(10, 8)
    assert tracker.process(10, 12) is True  # fires

    # the round just bet on is skipped, regardless of its own values
    assert tracker.process(999, 999) is False

    # next pair after the skip starts counting fresh from 0
    assert tracker.process(9, 9) is False
    assert tracker.process(9, 9) is True
    assert tracker.last_pair_values == [9, 9, 9, 9]


def test_pair_none_half_total_resets_the_pending_pair():
    tracker = RoundPairStreakTracker()
    tracker.process(9, 9)  # 1 round counted
    assert tracker.process(None, 9) is False  # resets, does not count as round 2

    # the discarded round doesn't carry over into the next pair
    assert tracker.process(9, 9) is False
    assert tracker.process(9, 9) is True


def test_pair_progress_reporting_through_counting_reset_skipped_armed():
    tracker = RoundPairStreakTracker()

    tracker.process(9, 9)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (1, 2, "counting")

    tracker.process(3, 3)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (0, 2, "reset")

    tracker.process(9, 9)
    tracker.process(9, 8)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (0, 3, "armed")

    tracker.process(999, 999)  # the skipped bet-target round
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (0, 3, "skipped")

    tracker.process(9, 9)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (1, 2, "counting")


def test_pair_thresholds_are_configurable():
    tracker = RoundPairStreakTracker(half_threshold=5, required_count=4)
    assert tracker.process(5, 5) is False  # 2 of 2 so far
    assert tracker.process(5, 4) is False  # values [5,5,5,4] -- only 3 of 4 qualify, required_count=4
    assert tracker.process(5, 5) is False
    assert tracker.process(5, 5) is True  # values [5,5,5,5] -- 4 of 4 qualify
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: the new `test_pair_*` tests fail with `ImportError: cannot import name 'RoundPairStreakTracker'`. All existing `PatternTracker` tests still pass.

- [ ] **Step 3: Implement `RoundPairStreakTracker`**

Append to the end of `services/bettor/pattern.py` (after the existing `PatternTracker` class — do not modify `PatternTracker` itself):

```python
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
```

- [ ] **Step 4: Run the tests to verify they all pass**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: all tests pass (36 original + 7 new = 43).

- [ ] **Step 5: Commit**

```bash
git add services/bettor/pattern.py tests/test_pattern.py
git commit -m "$(cat <<'EOF'
Add RoundPairStreakTracker for Pattern 4's pair-counting rule

Fires when >=3 of the 4 (1st-half-total, 2nd-half-total) values across
2 consecutive finished rounds are >=9. A new, separate class alongside
PatternTracker -- the pair-count shape doesn't fit as a parameterization
of PatternTracker's single-scalar consecutive-streak model.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Extend `BetExecutor.place_bet()` — main-game period + odds-wait

**Files:**
- Modify: `services/bettor/betting_api.py`
- Modify: `tests/test_betting_api.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `BetExecutor.place_bet(..., period: Literal[0, 1, 2] = 1, ..., min_odds: float | None = None, poll_interval_seconds: float = 5.0, is_stale: Callable[[], bool] | None = None) -> BetResult`. `period=0` targets the raw `match_id` (no offset). `min_odds=None` (Patterns 1–3's implicit default) is byte-identical to today's single-shot behavior. Consumed by Task 5 (Pattern 4's `place_call`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_betting_api.py`:

```python
def test_period_0_targets_the_raw_match_id():
    seen_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            seen_ids.append(int(request.url.params["id"]))
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5)]))
        body = json.loads(request.content)
        seen_ids.append(body["Events"][0]["GameId"])
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=751444117, home="A", away="B", stake=90, line=16.5, period=0, over=False,
        )
    )
    asyncio.run(executor.aclose())

    assert seen_ids == [751444117, 751444117]  # raw match_id, no offset
    assert result.success is True


async def _instant_sleep(_seconds: float) -> None:
    return None


def test_min_odds_waits_for_odds_to_clear_the_threshold(monkeypatch):
    # Body asserted after asyncio.run(), not inside the handler -- place_bet()
    # wraps the whole request in a broad `except Exception`, so an in-handler
    # AssertionError would get swallowed and reported as a misleading
    # "request failed: ..." BetResult instead of the real failure (see
    # test_bet_type_override_ignores_the_over_flag's comment above).
    monkeypatch.setattr("services.bettor.betting_api.asyncio.sleep", _instant_sleep)
    odds_sequence = [1.2, 1.3, 1.6]
    calls = {"odds": 0}
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            coef = odds_sequence[min(calls["odds"], len(odds_sequence) - 1)]
            calls["odds"] += 1
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5, coef=coef)]))
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=16.5, period=0, over=False, min_odds=1.5,
        )
    )
    asyncio.run(executor.aclose())

    assert calls["odds"] == 3
    assert result.success is True
    assert result.odds == 1.6
    assert captured["body"]["Events"][0]["Coef"] == 1.6


def test_min_odds_already_met_places_immediately_without_is_stale():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5, coef=1.8)]))
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=16.5, period=0, over=False, min_odds=1.5,
        )
    )
    asyncio.run(executor.aclose())

    assert result.success is True
    assert result.odds == 1.8


def test_min_odds_never_reached_gives_up_once_stale(monkeypatch):
    # Odds never clear 1.5 in this test, so MakeBetWeb should never be
    # called -- the handler only ever needs to serve GetGameZip.
    monkeypatch.setattr("services.bettor.betting_api.asyncio.sleep", _instant_sleep)
    calls = {"odds": 0}
    stale_after = 3

    def handler(request: httpx.Request) -> httpx.Response:
        calls["odds"] += 1
        return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5, coef=1.2)]))

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=16.5, period=0, over=False,
            min_odds=1.5,
            is_stale=lambda: calls["odds"] >= stale_after,
        )
    )
    asyncio.run(executor.aclose())

    assert calls["odds"] == stale_after
    assert result.success is False
    assert "market closed before odds reached 1.5" in result.reason


def test_min_odds_none_never_sleeps(monkeypatch):
    async def _sleep_that_fails(_seconds: float) -> None:
        raise AssertionError("place_bet() must not sleep when min_odds is None")

    monkeypatch.setattr("services.bettor.betting_api.asyncio.sleep", _sleep_that_fails)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/GetGameZip")
        return httpx.Response(200, json=_game_zip_response([]))  # no matching market

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90, line=6.5))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert result.reason == "market not open (stale-fire guard)"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_betting_api.py -v`
Expected: the 5 new tests fail — `test_period_0_...` with a `KeyError`/wrong `GameId` assertion (period=0 currently falls into the `else` branch and resolves to `SECOND_HALF_ID_OFFSET`), the `min_odds` ones with `TypeError: place_bet() got an unexpected keyword argument 'min_odds'`. All other existing tests still pass.

- [ ] **Step 3: Implement both extensions**

In `services/bettor/betting_api.py`, replace the import block:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
```

with:

```python
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal
```

Replace:

```python
FIRST_HALF_ID_OFFSET = 1
SECOND_HALF_ID_OFFSET = 2
TOTALS_GROUP = 17
```

with:

```python
MAIN_GAME_ID_OFFSET = 0  # the raw match_id itself -- see the module comment above about the three sub-game ids
FIRST_HALF_ID_OFFSET = 1
SECOND_HALF_ID_OFFSET = 2
TOTALS_GROUP = 17
```

Replace the `place_bet` signature and its first block:

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

        try:
            auth = await self._auth_reader()
        except Exception as exc:
            return BetResult(success=False, reason=f"auth read failed: {exc}")

        try:
            coef = await self._current_odds(game_id, line, bet_type, group)
        except Exception as exc:
            return BetResult(success=False, reason=f"odds lookup failed: {exc}")
        if coef is None:
            return BetResult(success=False, reason="market not open (stale-fire guard)")

        headers = {
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
        period: Literal[0, 1, 2] = 1,
        over: bool = True,
        bet_type: int | None = None,
        group: int = TOTALS_GROUP,
        min_odds: float | None = None,
        poll_interval_seconds: float = 5.0,
        is_stale: Callable[[], bool] | None = None,
    ) -> BetResult:
        """`bet_type`/`group` default to Pattern 1/2's Total-market shape,
        derived from `over` exactly as before. Pass both explicitly (as
        Pattern 3 does, for the Double Chance market) to bet a market with
        no over/under concept at all -- in that case `over` is ignored.

        `period=0` targets the raw `match_id` ("Main game", used by
        Pattern 4) instead of a half-scoped sub-game id.

        `min_odds` (Pattern 4 only) makes this poll `_current_odds()` in a
        loop -- checking `is_stale()` each iteration -- until the odds are
        >= min_odds, then submits at that same fetched price. Leaving it
        None (Patterns 1-3's implicit default) reproduces today's exact
        single-shot behavior: a single odds fetch, bailing immediately if
        it's None. This wait has no timeout of its own beyond `is_stale()`
        eventually returning True."""
        if period == 0:
            offset = MAIN_GAME_ID_OFFSET
        elif period == 1:
            offset = FIRST_HALF_ID_OFFSET
        else:
            offset = SECOND_HALF_ID_OFFSET
        game_id = match_id + offset
        if bet_type is None:
            bet_type = TOTAL_OVER_T if over else TOTAL_UNDER_T

        try:
            auth = await self._auth_reader()
        except Exception as exc:
            return BetResult(success=False, reason=f"auth read failed: {exc}")

        while True:
            try:
                coef = await self._current_odds(game_id, line, bet_type, group)
            except Exception as exc:
                return BetResult(success=False, reason=f"odds lookup failed: {exc}")
            if coef is not None and (min_odds is None or coef >= min_odds):
                break
            if min_odds is None:
                return BetResult(success=False, reason="market not open (stale-fire guard)")
            if is_stale is not None and is_stale():
                return BetResult(
                    success=False,
                    reason=f"market closed before odds reached {min_odds} (last seen: {coef})",
                )
            await asyncio.sleep(poll_interval_seconds)

        headers = {
```

- [ ] **Step 4: Run the tests to verify they all pass**

Run: `.venv/bin/pytest tests/test_betting_api.py -v`
Expected: all tests pass (16 original + 5 new = 21).

- [ ] **Step 5: Commit**

```bash
git add services/bettor/betting_api.py tests/test_betting_api.py
git commit -m "$(cat <<'EOF'
Extend BetExecutor.place_bet with period=0 and an odds-wait loop

period widens to Literal[0, 1, 2] -- 0 targets the raw match_id ("Main
game", Pattern 4's market) via a new MAIN_GAME_ID_OFFSET = 0. New
min_odds/poll_interval_seconds/is_stale parameters make place_bet poll
until the odds clear a minimum before submitting -- min_odds=None
(Patterns 1-3's implicit default) is byte-identical to today's
single-shot behavior. Auth is still read before any network call, so
an auth failure still short-circuits before the odds lookup for every
caller.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: New `PATTERN4_*` config knobs

**Files:**
- Modify: `shared/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `Config.pattern4_bet_line: float`, `Config.pattern4_bet_stake_amount: float`, `Config.pattern4_enabled: bool` — consumed by Task 5.

- [ ] **Step 1: Add the new fields to the `Config` dataclass**

In `shared/config.py`, replace:

```python
    pattern3_streak_length: int
    pattern3_bet_stake_amount: float
    pattern3_enabled: bool
    cdp_url: str
```

with:

```python
    pattern3_streak_length: int
    pattern3_bet_stake_amount: float
    pattern3_enabled: bool
    pattern4_bet_line: float
    pattern4_bet_stake_amount: float
    pattern4_enabled: bool
    cdp_url: str
```

- [ ] **Step 2: Populate them in `load_config()`**

Replace:

```python
        pattern3_streak_length=int(os.environ.get("PATTERN3_STREAK_LENGTH", "2")),
        pattern3_bet_stake_amount=float(os.environ.get("PATTERN3_BET_STAKE_AMOUNT", "90")),
        pattern3_enabled=_bool("PATTERN3_ENABLED", True),
        cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
```

with:

```python
        pattern3_streak_length=int(os.environ.get("PATTERN3_STREAK_LENGTH", "2")),
        pattern3_bet_stake_amount=float(os.environ.get("PATTERN3_BET_STAKE_AMOUNT", "90")),
        pattern3_enabled=_bool("PATTERN3_ENABLED", True),
        pattern4_bet_line=float(os.environ.get("PATTERN4_BET_LINE", "16.5")),
        pattern4_bet_stake_amount=float(os.environ.get("PATTERN4_BET_STAKE_AMOUNT", "90")),
        pattern4_enabled=_bool("PATTERN4_ENABLED", True),
        cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
```

- [ ] **Step 3: Verify defaults load cleanly**

Run: `.venv/bin/python -c "from shared.config import load_config; c = load_config(); print(c.pattern4_bet_line, c.pattern4_bet_stake_amount, c.pattern4_enabled)"`
Expected: `16.5 90.0 True`

- [ ] **Step 4: Document the new variables in `.env.example`**

In `.env.example`, insert immediately after the existing `PATTERN3_ENABLED=true` line and before the `# --- Betting: automatic session recovery (services/bettor) ---` comment:

```bash

# --- Betting: Pattern 4 — "Main Game Under 16.5" pair streak (services/bettor) ---
# Watches every finished round's 1st-half total and 2nd-half total (2
# values per round). Groups rounds into non-overlapping pairs of 2 (4
# values). Fires when >= 3 of those 4 values are >= 9; bets on the next
# round's Total. Main game market, Under PATTERN4_BET_LINE. Same
# fire -> skip-one-round -> restart life cycle as Patterns 1-3, applied
# to a pair-based counter instead of a single-scalar streak -- see
# services/bettor/pattern.py's RoundPairStreakTracker. Real money, live
# from the first run if PATTERN4_ENABLED is true -- see README.md
# "Betting patterns" before changing these. Runs as a fourth,
# independent pattern in the same services/bettor process as Pattern
# 1/2/3 -- no separate service to start. Unlike Patterns 1-3, this
# pattern's BetExecutor call waits for the live odds to reach 1.5 before
# submitting (hardcoded, not env-configurable) -- see the design spec
# for why, and note it does NOT participate in the other patterns'
# mutual-exclusion check (different market, no shared risk).
PATTERN4_BET_LINE=16.5
PATTERN4_BET_STAKE_AMOUNT=90
# Kill switch for Pattern 4 only -- Pattern 1/2/3 are unaffected either
# way. Defaults to true (Pattern 4 goes live immediately) if unset. Set
# to false to pause Pattern 4: it keeps tracking the pair and publishes
# normal PatternProgress on non-firing rounds, but on a firing round it
# only logs a warning ("PATTERN 4 ARMED ... suppressing bet placement")
# instead of publishing PatternArmed or arming a target/calling
# BetExecutor, so it cannot place a bet while off.
PATTERN4_ENABLED=true
```

- [ ] **Step 5: Commit**

```bash
git add shared/config.py .env.example
git commit -m "$(cat <<'EOF'
Add config knobs for the main-game-under-16.5 pair streak pattern

PATTERN4_BET_LINE, PATTERN4_BET_STAKE_AMOUNT, PATTERN4_ENABLED -- same
load_config()/.env convention as Patterns 1-3's own knobs, fully
independent of them. The pair rule's own thresholds (half_threshold=9,
required_count=3) and the odds-wait's min_odds=1.5 stay hardcoded, per
the design spec's explicit decision.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Widen `PatternProgress` schema + `render_pattern_progress`'s `pair_count` branch

**Files:**
- Modify: `shared/events.py`
- Modify: `services/display/render.py`

**Interfaces:**
- Produces: `PatternProgress.direction` gains `"pair_count"`; `PatternProgress.outcome` gains `"counting"`. No new fields, no new event kinds. Consumed by Task 5 (`main.py` publishes `PatternProgress(direction="pair_count", ...)` for Pattern 4).

- [ ] **Step 1: Widen the two `Literal`s in `shared/events.py`**

Replace:

```python
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
```

with:

```python
class PatternProgress(BaseModel):
    """Emitted after every finished round is fed into the streak tracker,
    whether or not it moved the streak toward firing — lets a display
    narrate the pattern's life cycle round by round instead of only at
    the moment it fires (see PatternArmed for that moment).

    "pair_count" (Pattern 4) and "counting" are additive: Pattern 4's
    RoundPairStreakTracker doesn't have a single-scalar per-round
    qualify/reset shape the way Patterns 1-3's direction values do, so
    forcing it into one of the existing three directions would
    misrepresent what actually happened that round."""

    kind: Literal["pattern_progress"] = "pattern_progress"
    match_id: int
    pattern_name: str
    direction: Literal["at_or_under", "at_or_over", "equals", "pair_count"]
    streak: int
    streak_length: int
    threshold: int | str
    total: int | str | None
    outcome: Literal["qualifying", "reset", "skipped", "counting"]
```

- [ ] **Step 2: Sanity-check the model round-trips through JSON with the new literal values**

Run:
```bash
.venv/bin/python -c "
from shared.events import PatternProgress
p = PatternProgress(match_id=1, pattern_name='main_game_under_16.5_streak', direction='pair_count', streak=1, streak_length=2, threshold=9, total=2, outcome='counting')
print(PatternProgress.model_validate_json(p.model_dump_json()))
"
```
Expected: the reconstructed object prints, no exception.

- [ ] **Step 3: Add the `pair_count` branch to `render_pattern_progress`**

In `services/display/render.py`, replace:

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
```

with:

```python
def render_pattern_progress(event: PatternProgress) -> None:
    """One dim line narrating what the just-finished round did to the
    streak — printed for every round (see render_pattern_armed for the
    moment it actually fires, which this deliberately doesn't duplicate).
    `direction` picks the qualify/reset comparison wording so this reads
    correctly for Pattern 1 (at_or_under), Pattern 2 (at_or_over),
    Pattern 3 (equals, a categorical match rather than a numeric one),
    and Pattern 4 (pair_count, a 2-round value-count rule rather than a
    per-round comparison — handled in its own early-return branch since
    it doesn't share the qualify/reset comparison shape at all)."""
    if event.direction == "pair_count":
        if event.outcome == "skipped":
            detail = "skipped — bet target"
        elif event.outcome == "counting":
            detail = f"{event.total} value(s) >= {event.threshold} so far this pair"
        else:  # reset -- the pair completed without meeting required_count
            detail = f"{event.total} of 4 values >= {event.threshold}, resets"
        console.print(
            f"[bold bright_red]{event.pattern_name} pair: {event.streak}/{event.streak_length} rounds ({detail})[/bold bright_red]"
        )
        return

    if event.direction == "equals":
        qualify_cmp, reset_cmp = "==", "!="
    elif event.direction == "at_or_under":
        qualify_cmp, reset_cmp = "≤", ">"
    else:
        qualify_cmp, reset_cmp = "≥", "<"
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
```

(`render_pattern_armed`/`render_bet_placed`/`render_bet_failed`/`render_bet_settled`/`log_bet_event` need no changes — they already print `market_label`/`condition_label`/`period_total` verbatim, generalized during Pattern 2/3's work.)

- [ ] **Step 4: Smoke-test the render module still imports and handles a `pair_count` event**

Run:
```bash
.venv/bin/python -c "
from services.display.render import render_pattern_progress
from shared.events import PatternProgress
render_pattern_progress(PatternProgress(match_id=1, pattern_name='main_game_under_16.5_streak', direction='pair_count', streak=1, streak_length=2, threshold=9, total=2, outcome='counting'))
render_pattern_progress(PatternProgress(match_id=1, pattern_name='main_game_under_16.5_streak', direction='pair_count', streak=0, streak_length=2, threshold=9, total=2, outcome='reset'))
render_pattern_progress(PatternProgress(match_id=1, pattern_name='main_game_under_16.5_streak', direction='pair_count', streak=0, streak_length=2, threshold=9, total=3, outcome='skipped'))
"
```
Expected: three printed lines, no exception.

Run the full existing test suite to confirm nothing else broke:
Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add shared/events.py services/display/render.py
git commit -m "$(cat <<'EOF'
Add pair_count/counting to PatternProgress for Pattern 4

Additive Literal values only -- no new fields, no new event kinds.
render_pattern_progress gets an early-return branch for pair_count
since Pattern 4's 2-round value-count rule doesn't share the
qualify/reset per-round comparison shape Patterns 1-3's directions do.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire Pattern 4 into `services/bettor/main.py`

**This task touches the live, real-money `consume()` loop.** It must leave `main.py` fully consistent at the end — all four patterns wired, all existing tests still passing, since a restart of `services/bettor/main.py` after this task runs all four patterns for real.

**Files:**
- Modify: `services/bettor/main.py`

**Interfaces:**
- Consumes: `RoundPairStreakTracker` (Task 1), `BetExecutor.place_bet(period=0, min_odds=..., poll_interval_seconds=..., is_stale=...)` (Task 2), `config.pattern4_*` (Task 3), `PatternProgress(direction="pair_count", outcome="counting")` (Task 4).
- Produces: the full four-pattern `consume()` loop — `python -m services.bettor.main` now runs all four patterns.

- [ ] **Step 1: Update the `pattern` import**

Replace:

```python
from services.bettor.pattern import PatternTracker
```

with:

```python
from services.bettor.pattern import PatternTracker, RoundPairStreakTracker
```

- [ ] **Step 2: Add Pattern 4's name/constant block**

Replace:

```python
PATTERN1_NAME = "1st_half_over_6.5_streak"
PATTERN2_NAME = "2nd_half_under_7.5_streak"
PATTERN3_NAME = "1st_half_winner_2x_streak"
```

with:

```python
PATTERN1_NAME = "1st_half_over_6.5_streak"
PATTERN2_NAME = "2nd_half_under_7.5_streak"
PATTERN3_NAME = "1st_half_winner_2x_streak"
PATTERN4_NAME = "main_game_under_16.5_streak"
PATTERN4_MIN_ODDS = 1.5
PATTERN4_ODDS_POLL_INTERVAL_SECONDS = 5.0
```

- [ ] **Step 3: Widen `_market_label` to a 3-way period mapping**

Replace:

```python
def _market_label(period: int, over: bool, line: float) -> str:
    half = "1st half" if period == 1 else "2nd half"
    side = "Over" if over else "Under"
    return f"Total. {half} {side} {line:g}"
```

with:

```python
def _market_label(period: int, over: bool, line: float) -> str:
    half = {0: "Main game", 1: "1st half", 2: "2nd half"}[period]
    side = "Over" if over else "Under"
    return f"Total. {half} {side} {line:g}"
```

- [ ] **Step 4: Add `_pair_condition_label`**

Replace:

```python
def _winner_condition_label(streak_length: int) -> str:
    return f"{streak_length} consecutive rounds with 1st half winner 2X"
```

with:

```python
def _winner_condition_label(streak_length: int) -> str:
    return f"{streak_length} consecutive rounds with 1st half winner 2X"


def _pair_condition_label() -> str:
    return "2-round pair with >=3 of 4 half-totals >= 9"
```

- [ ] **Step 5: Run tests + import check before wiring `run()` — confirms Steps 1-4 didn't break anything**

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass (no behavior change yet — `run()` itself is untouched so far).

Run: `.venv/bin/python -c "import services.bettor.main"`
Expected: no exception.

- [ ] **Step 6: Construct `tracker4`/`targets4`/`placed_matches4`/`pattern4_tasks` in `run()`**

Replace:

```python
    tracker3 = PatternTracker(
        threshold="2X",
        streak_length=config.pattern3_streak_length,
        direction="equals",
    )
    targets3 = TargetTracker()
    placed_matches3: set[int] = set()

    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)
```

with:

```python
    tracker3 = PatternTracker(
        threshold="2X",
        streak_length=config.pattern3_streak_length,
        direction="equals",
    )
    targets3 = TargetTracker()
    placed_matches3: set[int] = set()

    tracker4 = RoundPairStreakTracker(half_threshold=9, required_count=3)
    targets4 = TargetTracker(stale_statuses={"finished"})
    placed_matches4: set[int] = set()
    # place() is spawned as a task (not awaited) for Pattern 4 only, since
    # its odds-wait can span an entire match -- these references keep the
    # tasks from being garbage-collected mid-flight (a real asyncio
    # requirement, not optional bookkeeping).
    pattern4_tasks: list[asyncio.Task] = []

    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)
```

- [ ] **Step 7: Add Pattern 4's startup log line + the task exception-logging callback**

Replace:

```python
    log.info(
        f"starting Pattern 3 — streak_length={config.pattern3_streak_length} "
        f"trigger=2X selection=1X stake={config.pattern3_bet_stake_amount} "
        f"{'ENABLED' if config.pattern3_enabled else 'DISABLED (PATTERN3_ENABLED=false) — tracking only, will not bet'}"
    )
    log.info(
        f"starting session watchdog — check_interval={config.auth_watchdog_check_interval_seconds:g}s "
        f"cooldown={config.auth_watchdog_login_cooldown_seconds:g}s "
        f"{'ENABLED' if config.auth_watchdog_enabled else 'DISABLED (AUTH_WATCHDOG_ENABLED=false)'}"
    )

    async def place(
```

with:

```python
    log.info(
        f"starting Pattern 3 — streak_length={config.pattern3_streak_length} "
        f"trigger=2X selection=1X stake={config.pattern3_bet_stake_amount} "
        f"{'ENABLED' if config.pattern3_enabled else 'DISABLED (PATTERN3_ENABLED=false) — tracking only, will not bet'}"
    )
    log.info(
        f"starting Pattern 4 — half_threshold=9 required_count=3(of 4) "
        f"bet_line={config.pattern4_bet_line} min_odds={PATTERN4_MIN_ODDS} "
        f"stake={config.pattern4_bet_stake_amount} "
        f"{'ENABLED' if config.pattern4_enabled else 'DISABLED (PATTERN4_ENABLED=false) — tracking only, will not bet'}"
    )
    log.info(
        f"starting session watchdog — check_interval={config.auth_watchdog_check_interval_seconds:g}s "
        f"cooldown={config.auth_watchdog_login_cooldown_seconds:g}s "
        f"{'ENABLED' if config.auth_watchdog_enabled else 'DISABLED (AUTH_WATCHDOG_ENABLED=false)'}"
    )

    def _log_pattern4_task_exception(task: asyncio.Task) -> None:
        """A bare fire-and-forget asyncio.Task otherwise swallows an
        unexpected exception silently -- every other pattern's failures
        already surface via BetFailed/log lines, so this one should too."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(f"pattern4 placement task failed: {exc}")

    async def place(
```

- [ ] **Step 8: Wire `targets4.on_discovered` into the `MatchDiscovered` branch**

Replace:

```python
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
```

with:

```python
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
                    target4 = targets4.on_discovered(event)
                    if target4 is not None and config.pattern4_enabled:
                        task = asyncio.create_task(
                            place(
                                target4,
                                targets=targets4,
                                other_patterns=[],
                                placed_matches=placed_matches4,
                                market_label=_market_label(0, False, config.pattern4_bet_line),
                                stale_period_label="match end",
                                line=config.pattern4_bet_line,
                                stake=config.pattern4_bet_stake_amount,
                                place_call=lambda: executor.place_bet(
                                    match_id=target4.match_id,
                                    home=target4.home,
                                    away=target4.away,
                                    stake=config.pattern4_bet_stake_amount,
                                    line=config.pattern4_bet_line,
                                    period=0,
                                    over=False,
                                    min_odds=PATTERN4_MIN_ODDS,
                                    poll_interval_seconds=PATTERN4_ODDS_POLL_INTERVAL_SECONDS,
                                    is_stale=lambda: targets4.is_stale(target4.match_id),
                                ),
                            )
                        )
                        pattern4_tasks.append(task)
                        task.add_done_callback(_log_pattern4_task_exception)
                elif isinstance(event, MatchStarted):
                    targets.on_started(event.match_id)
                    targets2.on_started(event.match_id)
                    targets3.on_started(event.match_id)
                    targets4.on_started(event.match_id)
```

- [ ] **Step 9: Wire `targets4.on_half_time` into the `MatchHalfTime` branch**

Replace:

```python
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                    targets2.on_half_time(event.match_id)
                    targets3.on_half_time(event.match_id)
```

with:

```python
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                    targets2.on_half_time(event.match_id)
                    targets3.on_half_time(event.match_id)
                    targets4.on_half_time(event.match_id)
```

(`"half_time"` isn't in Pattern 4's `stale_statuses={"finished"}`, so this call is bookkeeping parity only — harmless, keeps `targets4.debug_state()` accurate.)

- [ ] **Step 10: Wire Pattern 4's evaluation + settlement into the `MatchFinished` branch**

Replace:

```python
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)
                    targets2.on_finished(event.match_id)
                    targets3.on_finished(event.match_id)

                    second_half_total = (
                        None if event.second_half is None
                        else event.second_half.home_goals + event.second_half.away_goals
                    )

                    if event.match_id in placed_matches2 and second_half_total is not None:
```

with:

```python
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)
                    targets2.on_finished(event.match_id)
                    targets3.on_finished(event.match_id)
                    targets4.on_finished(event.match_id)

                    second_half_total = (
                        None if event.second_half is None
                        else event.second_half.home_goals + event.second_half.away_goals
                    )
                    first_half_total = (
                        None if event.first_half is None
                        else event.first_half.home_goals + event.first_half.away_goals
                    )

                    if event.match_id in placed_matches2 and second_half_total is not None:
```

Then replace the tail of the `MatchFinished` branch (Pattern 2's closing `PatternProgress` publish, immediately followed by the outer `except`):

```python
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
```

with:

```python
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

                    if event.match_id in placed_matches4:
                        await bus.publish(
                            config.channel_match_events,
                            BetSettled(
                                match_id=event.match_id,
                                home=event.home,
                                away=event.away,
                                won=event.total_goals < config.pattern4_bet_line,
                                period_total=event.total_goals,
                                market_label=_market_label(0, False, config.pattern4_bet_line),
                            ),
                        )

                    fired4 = tracker4.process(first_half_total, second_half_total)
                    if fired4:
                        if not config.pattern4_enabled:
                            log.warning(
                                f"PATTERN 4 ARMED — pair values {tracker4.last_pair_values} "
                                "— but PATTERN4_ENABLED=false, suppressing bet placement"
                            )
                        else:
                            log.info(f"PATTERN 4 ARMED — pair values {tracker4.last_pair_values}")
                            await bus.publish(
                                config.channel_match_events,
                                PatternArmed(
                                    pattern_name=PATTERN4_NAME,
                                    qualifying_totals=list(tracker4.last_pair_values),
                                    market_label=_market_label(0, False, config.pattern4_bet_line),
                                    condition_label=_pair_condition_label(),
                                ),
                            )
                            target4 = targets4.arm()
                            if target4 is not None:
                                task = asyncio.create_task(
                                    place(
                                        target4,
                                        targets=targets4,
                                        other_patterns=[],
                                        placed_matches=placed_matches4,
                                        market_label=_market_label(0, False, config.pattern4_bet_line),
                                        stale_period_label="match end",
                                        line=config.pattern4_bet_line,
                                        stake=config.pattern4_bet_stake_amount,
                                        place_call=lambda: executor.place_bet(
                                            match_id=target4.match_id,
                                            home=target4.home,
                                            away=target4.away,
                                            stake=config.pattern4_bet_stake_amount,
                                            line=config.pattern4_bet_line,
                                            period=0,
                                            over=False,
                                            min_odds=PATTERN4_MIN_ODDS,
                                            poll_interval_seconds=PATTERN4_ODDS_POLL_INTERVAL_SECONDS,
                                            is_stale=lambda: targets4.is_stale(target4.match_id),
                                        ),
                                    )
                                )
                                pattern4_tasks.append(task)
                                task.add_done_callback(_log_pattern4_task_exception)
                            else:
                                log.info(f"PATTERN 4 fired but no target yet — {targets4.debug_state()}")
                    else:
                        await bus.publish(
                            config.channel_match_events,
                            PatternProgress(
                                match_id=event.match_id,
                                pattern_name=PATTERN4_NAME,
                                direction="pair_count",
                                streak=tracker4.rounds_in_pair,
                                streak_length=2,
                                threshold=9,
                                total=tracker4.last_qualifying_count,
                                outcome=tracker4.last_outcome,
                            ),
                        )
            except Exception as err:  # noqa: BLE001 — one bad event must not kill the subscription
```

- [ ] **Step 11: Cancel `pattern4_tasks` on shutdown, consistent with `consumer_task`/`watchdog_task`**

Replace:

```python
    log.info("shutting down...")
    consumer_task.cancel()
    if watchdog_task is not None:
        watchdog_task.cancel()
    await executor.aclose()
    await bus.close()
```

with:

```python
    log.info("shutting down...")
    consumer_task.cancel()
    if watchdog_task is not None:
        watchdog_task.cancel()
    for task in pattern4_tasks:
        if not task.done():
            task.cancel()
    await executor.aclose()
    await bus.close()
```

- [ ] **Step 12: Run the full test suite + import check**

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass (no test directly exercises `main.py`'s `consume()` loop — this confirms Tasks 1-4's pieces individually still pass; `main.py`'s own correctness is verified by the manual restart in Task 7).

Run: `.venv/bin/python -c "import services.bettor.main"`
Expected: no exception.

- [ ] **Step 13: Commit**

```bash
git add services/bettor/main.py
git commit -m "$(cat <<'EOF'
Wire Pattern 4 ("Main Game Under 16.5" pair streak) into the bettor loop

Fourth, independent handler set: own tracker4 (RoundPairStreakTracker),
targets4 (TargetTracker(stale_statuses={"finished"})), placed_matches4.
No mutual exclusion with Patterns 1-3 in either direction -- different
market, no shared risk. Pattern 4's place() calls run as
asyncio.create_task(...) rather than being awaited inline, since its
odds-wait can span an entire match and must not block the other three
patterns' event processing; pattern4_tasks holds references so tasks
aren't garbage-collected mid-flight, with a done-callback that logs any
unexpected exception.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Document Pattern 4 in `README.md`

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing new (documentation only).

- [ ] **Step 1: Add a "Pattern 4" subsection after Pattern 3's**

In `README.md`, immediately after the existing Pattern 3 subsection (right before the `## Automatic session recovery` heading), insert:

```markdown
### Pattern 4 — "Main Game Under 16.5" pair streak

A different streak shape from Patterns 1-3, run as a fourth, independent
pattern in the same `services/bettor/` process — no separate service, no
separate CDP session. It watches every finished round's **1st-half total**
and **2nd-half total** (2 values per round) and groups rounds into
non-overlapping pairs of 2 (4 values per pair). When **>= 3 of those 4
values are >= 9**, it fires a real bet — `PATTERN4_BET_STAKE_AMOUNT`
(default 90, FCFA, independently configurable from Patterns 1-3's stakes)
on the *next* round's `Total. Main game` market, `Under PATTERN4_BET_LINE`
(default 16.5). Same fire → skip-one-round → restart life cycle as
Patterns 1-3, applied to a pair-based counter
(`services/bettor/pattern.py`'s `RoundPairStreakTracker`) instead of a
single-scalar streak — see that class's docstring for the exact mechanics,
and the design spec for the worked example this rule is based on.

Because this rule needs both halves of a round, Pattern 4 evaluates at
each round's `MatchFinished` event (like Pattern 2), not `MatchHalfTime`.

**Odds-gated placement — unique to Pattern 4.** Every other pattern places
its bet at whatever price `BetExecutor` finds the instant it looks.
Pattern 4 instead waits: once armed and targeting a match, it polls the
live odds every 5 seconds and only submits once they reach **1.5**,
placing at that price the moment it's seen. This wait has no timeout of
its own — it only stops when the odds clear 1.5 or the match itself
finishes (in which case no bet is placed for that fire, logged, not
alerted). Betting the whole match instead of a half also required a new
main-game sub-game id (`period=0` in `BetExecutor.place_bet()`, offset 0 —
the raw `match_id` itself, unlike the `+1`/`+2` half-scoped ids Patterns
1-3 use).

Because this wait can span an entire match, Pattern 4's bet placement runs
as a background task rather than blocking the shared event loop the way
Patterns 1-3's (effectively instant) placements do — so a slow Pattern 4
odds-wait on one match never delays another pattern's bet on a different
match.

**No mutual exclusion with Patterns 1-3, in either direction** — Pattern 4
bets `Total. Main game`, a market none of the other three touch, so
there's no shared-risk reason to block it, and it doesn't block them
either. It can stack a bet on the same round Pattern 1/2/3 also bets on.

Config knobs: `PATTERN4_BET_LINE`, `PATTERN4_BET_STAKE_AMOUNT`,
`PATTERN4_ENABLED` — see `.env.example`. The pair rule's own thresholds
(9, and "3 of 4") and the odds-wait's minimum (1.5) are hardcoded, not
env-configurable — an explicit decision, this being one precisely-defined
rule rather than a tunable family like Pattern 1/2's threshold +
streak_length. Shares `CDP_URL` and `BETS_LOG_PATH` with Pattern 1/2/3.
```

- [ ] **Step 2: Add the new config knobs to the "Configuration reference" table**

Replace:

```markdown
| `PATTERN3_ENABLED` | `true` | Kill switch for Pattern 3 only — set `false` to keep tracking the streak (normal `PatternProgress` still publishes on non-firing rounds) without ever placing a bet; a firing round while disabled only logs a warning instead of publishing `PatternArmed`. |
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the already-logged-in browser. Bet placement itself only ever reads fresh auth from it (cookies/localStorage, no UI automation); the session watchdog is the one thing that drives real form input on it, for login recovery only — see [Automatic session recovery](#automatic-session-recovery). |
```

with:

```markdown
| `PATTERN3_ENABLED` | `true` | Kill switch for Pattern 3 only — set `false` to keep tracking the streak (normal `PatternProgress` still publishes on non-firing rounds) without ever placing a bet; a firing round while disabled only logs a warning instead of publishing `PatternArmed`. |
| `PATTERN4_BET_LINE` | `16.5` | The Under line bet on in `Total. Main game`. |
| `PATTERN4_BET_STAKE_AMOUNT` | `90` | FCFA staked per fired Pattern 4 bet. |
| `PATTERN4_ENABLED` | `true` | Kill switch for Pattern 4 only — same contract as `PATTERN3_ENABLED` above. |
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the already-logged-in browser. Bet placement itself only ever reads fresh auth from it (cookies/localStorage, no UI automation); the session watchdog is the one thing that drives real form input on it, for login recovery only — see [Automatic session recovery](#automatic-session-recovery). |
```

- [ ] **Step 3: Update the roadmap paragraph**

Replace:

```markdown
Extraction + real-time terminal display, plus three live betting patterns
(see [Betting patterns](#betting-patterns)), are all implemented. All
three patterns run in the same `services/bettor/` process, coordinated so
no two of them ever bet on the same match in the same round. Any future
pattern follows the same shape: extend `PatternTracker`/`TargetTracker`/
`BetExecutor` rather than forking them, and publish its own
`PatternArmed`/`Bet*` events onto the same `xbet.match_events` bus.
```

with:

```markdown
Extraction + real-time terminal display, plus four live betting patterns
(see [Betting patterns](#betting-patterns)), are all implemented. All four
patterns run in the same `services/bettor/` process; Patterns 1-3
coordinate so no two of them ever bet on the same match in the same round,
while Pattern 4 (a different market, no shared risk) runs independently of
that coordination. Any future pattern follows the same shape: extend
`PatternTracker`/`TargetTracker`/`BetExecutor` rather than forking them
(or add a new small tracker class, as Pattern 4's pair-count rule needed,
when the shape genuinely doesn't fit `PatternTracker`'s parameters), and
publish its own `PatternArmed`/`Bet*` events onto the same
`xbet.match_events` bus.
```

- [ ] **Step 4: Proofread**

Run: `grep -n "Pattern 4" README.md` and confirm every new reference reads correctly in context (open the file and skim each match).

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document Pattern 4 in README

Adds the "Main Game Under 16.5" pair streak subsection (pair-counting
rule, the odds-gated placement mechanism unique to this pattern, the
async-task placement reasoning, and its opt-out of mutual exclusion),
adds the PATTERN4_* rows to the configuration reference table, and
updates the roadmap's pattern count.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Final full-suite regression + first-live-fire watch checklist

Pattern 4 ships with `PATTERN4_ENABLED=true` (explicit prior decision) —
there is no authorization gate to hold open here, unlike a pattern that
ships disabled-by-default. This task confirms the whole four-pattern
system is consistent end to end, and leaves a concrete checklist for
watching Pattern 4's first real fire — both its bet-shape (`period=0`,
never used live before) and its genuinely new mechanism (the odds-wait
loop, and placement running as a background task) need real-world
confirmation beyond what the mocked unit tests can prove.

**Files:** none (verification only).

- [ ] **Step 1: Run the full test suite one more time from a clean state**

Run: `.venv/bin/pytest tests/ -v`
Expected: every test across `test_pattern.py`, `test_betting_api.py`, `test_targeting.py`, `test_markets.py`, and `test_session_watchdog.py` passes. Note the final total in the task's completion notes for the record.

- [ ] **Step 2: Confirm `git log` shows a clean, complete task sequence**

Run: `git log --oneline -10`
Expected: one commit per task above (7 total, this task's excepted since it makes no file changes), each with a clear, single-purpose message.

- [ ] **Step 3: Restart the bettor process and confirm all four patterns start cleanly**

Follow this repo's own runbook (`./run.sh status`, `tail logs/bettor.log` — see the top-level `CLAUDE.md` for this feature) to restart `services/bettor/main.py` onto the new four-pattern code. Confirm the log shows all four `starting Pattern N — ...` lines (Pattern 4's ending in `ENABLED`), and that `PatternProgress` events for all four pattern names — including `pair` lines for `main_game_under_16.5_streak` — appear as live rounds finish, with no `failed to process ...` error lines.

- [ ] **Step 4: Confirm Pattern 4's placement is genuinely non-blocking**

The first time `PATTERN 4 ARMED` appears in the log and a target resolves, watch `logs/bettor.log` for the following several minutes while Pattern 4's odds-wait is presumably still polling: confirm `PatternProgress`/`BetPlaced`/`BetFailed`/`BetSettled` lines for Patterns 1-3 continue to appear normally during that window (a live round finishing, a different pattern arming) — this is the concrete evidence that the `asyncio.create_task` placement (Task 5, Step 8/10) is actually working as designed, not silently serializing behind Pattern 4's wait.

- [ ] **Step 5: Leave a first-live-fire watch checklist for whoever is present when Pattern 4 first arms**

Because `period=0` (the raw `match_id`, "Main game") and the odds-wait loop have never placed a real bet before, the first live fire deserves the same scrutiny Pattern 1's original trial and Pattern 3's first bet got:

- Confirm the eventual `BET PLACED` (not `BET FAILED`) line shows a plausible odds value for a Main Game Under 16.5 selection at the moment it fired — cross-check against the live odds ladder shown in the terminal's scorecard for that match at roughly the same timestamp, the same sanity check Pattern 3's checklist used.
- Confirm there is a real, visible delay between `PATTERN 4 ARMED` and `BET PLACED` if the odds were initially below 1.5 — a `BET PLACED` immediately after `PATTERN 4 ARMED` only means the odds happened to already be >= 1.5 at that instant, not that the wait loop is broken; if odds are visibly below 1.5 in the terminal at arm time and a bet still appears immediately, that is a real bug (the wait loop skipped its own check) worth investigating with the systematic-debugging skill.
- If it instead fails with `"market closed before odds reached 1.5 (last seen: ...)"`, confirm the match had genuinely already finished at that point (check the RESULT panel) — that is the wait's intended give-up condition working correctly, not a bug.
- Once the match reaches `MatchFinished`, confirm the `SETTLED` line's `WON`/`LOST` call agrees with the actual final combined score shown in the `RESULT` panel for that round (`won=True` iff `total_home_goals + total_away_goals < 16.5`) — pure arithmetic, but confirming it against one real live round closes the loop the same way Pattern 3's checklist did for its own settlement logic.

No code changes are expected from this step — it's a verification checklist, not a gate that gets checked off by editing anything. If a real discrepancy turns up, treat it as a new bug to fix (systematic-debugging skill, not a patch bolted directly onto this plan).
