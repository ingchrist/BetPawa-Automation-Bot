# First Betting Pattern ("1st Half Over 6.5" streak) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the bot's first automated betting pattern — detect 3 consecutive finished rounds with a 1st-half combined goal total ≤ 6, then place a real bet on the next round's `Total. 1st half` market, `Over 6.5`, via the operator's already-logged-in Chrome over CDP.

**Architecture:** New `services/bettor/` process, wired exactly like `aggregator`/`display` — an independent asyncio service subscribing to `xbet.match_events` over Redis. `pattern.py` and `targeting.py` are pure, unit-tested state machines with no I/O; `betting_api.py` places bets via a direct authenticated HTTP call to the site's own API (reworked mid-implementation from an original Playwright-UI-click design — see Task 5), touching Playwright only for a lightweight, read-only CDP touch to source auth tokens fresh from the browser's cookies/localStorage before each bet. The service publishes four new event kinds back onto the same channel so `display` can render them and log them to `data/bets.log`, the same way it already owns `data/result.log`.

**Tech Stack:** Python 3.10, asyncio, pydantic, Redis pub/sub (existing `shared/bus.py`), `rich` (existing `services/display`), Playwright (`playwright.async_api`, connecting to an already-running Chrome via `connect_over_cdp` — no new browser is launched, no new login).

**Spec:** `docs/superpowers/specs/2026-09-09-first-half-over-pattern-bettor-design.md`

## Global Constraints

- Pattern: 3 consecutive finished rounds with 1st-half total ≤ 6 (`PATTERN_LOW_THRESHOLD`, default `6`) fires a bet on the next round's `Total. 1st half` `Over 6.5` (`PATTERN_BET_LINE`, default `6.5`). Streak length is `PATTERN_STREAK_LENGTH`, default `3`.
- After a fire, the streak resets to 0 and the very next finished round is excluded from counting (it's the one just bet on) — the round after that restarts the count from 0.
- Stake is `BET_STAKE_AMOUNT`, default `90` (FCFA), always configurable via env — never hardcoded at the call site.
- Bets go live from the very first run — no dry-run gate (explicit user decision).
- If the target match's 1st half has already ended (`half_time` or `finished` already observed for it) by the moment the bet is about to be attempted, skip it and log a `BetFailed` with the reason — never click into a closed/irrelevant market (explicit user decision).
- CDP endpoint: `CDP_URL`, default `http://127.0.0.1:9222` — connects to the operator's existing, already-logged-in Chrome; the service must never launch its own browser or attempt a new login.
- Target league is always FC 25. 3x3. Conference League, league id `2860561` (verified live during design) — the site path segment is `2860561-fc-25-3x3-conference-league`.
- `services/bettor` must follow the existing three-service conventions exactly: `shared/logging.get_logger(name, config.log_dir)`, `shared/bus.EventBus`, `shared/config.load_config()`, a `run()` coroutine with SIGINT/SIGTERM handling via `asyncio.Event`, entrypoint `python -m services.bettor.main`.

---

## Task 1: New event kinds in `shared/events.py`

**Files:**
- Modify: `shared/events.py`

**Interfaces:**
- Produces: `PatternArmed`, `BetPlaced`, `BetFailed`, `BetSettled` pydantic models; updated `MatchEvent` union and `MATCH_EVENT_TYPES` dict, used by every later task.

- [ ] **Step 1: Add the four new event models**

Add after `MatchFinished` (before the `MatchEvent` union at the bottom of the file):

```python
class PatternArmed(BaseModel):
    kind: Literal["pattern_armed"] = "pattern_armed"
    pattern_name: str
    qualifying_totals: list[int]


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

- [ ] **Step 2: Extend the union and the kind-dispatch dict**

Replace:

```python
MatchEvent = MatchDiscovered | MatchStarted | MatchHalfTime | MatchScoreChanged | MatchFinished

# kind -> model, used by the bus to parse an incoming domain event without
# the subscriber having to guess which of the five shapes it received.
MATCH_EVENT_TYPES: dict[str, type[BaseModel]] = {
    "discovered": MatchDiscovered,
    "started": MatchStarted,
    "half_time": MatchHalfTime,
    "score_changed": MatchScoreChanged,
    "finished": MatchFinished,
}
```

with:

```python
MatchEvent = (
    MatchDiscovered
    | MatchStarted
    | MatchHalfTime
    | MatchScoreChanged
    | MatchFinished
    | PatternArmed
    | BetPlaced
    | BetFailed
    | BetSettled
)

# kind -> model, used by the bus to parse an incoming domain event without
# the subscriber having to guess which of the nine shapes it received.
MATCH_EVENT_TYPES: dict[str, type[BaseModel]] = {
    "discovered": MatchDiscovered,
    "started": MatchStarted,
    "half_time": MatchHalfTime,
    "score_changed": MatchScoreChanged,
    "finished": MatchFinished,
    "pattern_armed": PatternArmed,
    "bet_placed": BetPlaced,
    "bet_failed": BetFailed,
    "bet_settled": BetSettled,
}
```

- [ ] **Step 3: Sanity-check the models round-trip through JSON**

Run: `.venv/bin/python -c "from shared.events import BetPlaced; e = BetPlaced(match_id=1, league_name='x', home='A', away='B', stake=90, line=6.5); print(BetPlaced.model_validate_json(e.model_dump_json()))"`
Expected: prints the reconstructed `BetPlaced(...)` object, no exception.

- [ ] **Step 4: Commit**

```bash
git add shared/events.py
git commit -m "$(cat <<'EOF'
Add PatternArmed/BetPlaced/BetFailed/BetSettled event kinds

Extends the existing match_events contract for the new bettor service,
following the same discriminated-union pattern as the five events already
there.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

---

## Task 2: New config knobs in `shared/config.py` + `.env.example`

**Files:**
- Modify: `shared/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `Config.bet_stake_amount: float`, `Config.pattern_streak_length: int`, `Config.pattern_low_threshold: int`, `Config.pattern_bet_line: float`, `Config.cdp_url: str`, `Config.bets_log_path: Path` — consumed by `services/bettor/main.py` (Task 6) and `services/display/main.py` (Task 7).

- [ ] **Step 1: Add the new fields to the `Config` dataclass**

In `shared/config.py`, add to the `Config` dataclass (after `result_log_txt_path: Path`):

```python
    bets_log_path: Path
    bet_stake_amount: float
    pattern_streak_length: int
    pattern_low_threshold: int
    pattern_bet_line: float
    cdp_url: str
```

- [ ] **Step 2: Populate them in `load_config()`**

Add to the `Config(...)` construction (after `result_log_txt_path=_path("RESULT_LOG_PATH", "data/result.log"),`):

```python
        bets_log_path=_path("BETS_LOG_PATH", "data/bets.log"),
        bet_stake_amount=float(os.environ.get("BET_STAKE_AMOUNT", "90")),
        pattern_streak_length=int(os.environ.get("PATTERN_STREAK_LENGTH", "3")),
        pattern_low_threshold=int(os.environ.get("PATTERN_LOW_THRESHOLD", "6")),
        pattern_bet_line=float(os.environ.get("PATTERN_BET_LINE", "6.5")),
        cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),
```

- [ ] **Step 3: Verify defaults load cleanly**

Run: `.venv/bin/python -c "from shared.config import load_config; c = load_config(); print(c.bet_stake_amount, c.pattern_streak_length, c.pattern_low_threshold, c.pattern_bet_line, c.cdp_url, c.bets_log_path)"`
Expected: `90.0 3 6 6.5 http://127.0.0.1:9222 <path>/data/bets.log`

- [ ] **Step 4: Document the new variables in `.env.example`**

Append to `.env.example`:

```bash
# --- Betting: Pattern 1 — "1st Half Over 6.5" streak (services/bettor) ---
# Fires after PATTERN_STREAK_LENGTH consecutive finished rounds each have a
# 1st-half combined goal total <= PATTERN_LOW_THRESHOLD; bets on the next
# round's Total. 1st half market, Over PATTERN_BET_LINE. Real money, live
# from the first run — see README.md "Betting patterns" before changing
# these.
BET_STAKE_AMOUNT=90
PATTERN_STREAK_LENGTH=3
PATTERN_LOW_THRESHOLD=6
PATTERN_BET_LINE=6.5
# Chrome DevTools Protocol endpoint for the already-running, already-logged-in
# browser the bettor drives — never a browser this bot launches itself.
CDP_URL=http://127.0.0.1:9222
# Plain-text audit trail of every pattern fire / bet placed / failed /
# settled, written by the display service, tracked in git like RESULT_LOG_PATH.
BETS_LOG_PATH=data/bets.log
```

- [ ] **Step 5: Commit**

```bash
git add shared/config.py .env.example
git commit -m "$(cat <<'EOF'
Add config knobs for the 1st-half-over streak betting pattern

BET_STAKE_AMOUNT, PATTERN_STREAK_LENGTH, PATTERN_LOW_THRESHOLD,
PATTERN_BET_LINE, CDP_URL, BETS_LOG_PATH — same load_config()/.env
convention as every other knob in the project.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

---

## Task 3: `PatternTracker` (pure state machine) + unit tests

**Files:**
- Create: `services/bettor/__init__.py` (empty)
- Create: `services/bettor/pattern.py`
- Create: `pytest.ini`
- Test: `tests/__init__.py` (empty)
- Test: `tests/test_pattern.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `PatternTracker(low_threshold: int = 6, streak_length: int = 3)` with `.process(first_half_total: int | None) -> bool` and `.last_streak_totals: list[int]` — consumed by `services/bettor/main.py` (Task 6).

- [ ] **Step 1: Add pytest to `requirements.txt` and install it**

Add a line to `requirements.txt`:

```
pytest>=8.0
```

Run: `.venv/bin/pip install -q -r requirements.txt`

- [ ] **Step 2: Add `pytest.ini` so `tests/` can import `services.*` and `shared.*`**

```ini
[pytest]
pythonpath = .
testpaths = tests
```

- [ ] **Step 3: Create the empty package/test-package markers**

```bash
mkdir -p services/bettor tests
touch services/bettor/__init__.py tests/__init__.py
```

- [ ] **Step 4: Write the failing tests**

`tests/test_pattern.py`:

```python
from services.bettor.pattern import PatternTracker


def test_fires_on_three_consecutive_qualifying_rounds():
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True
    assert tracker.last_streak_totals == [4, 5, 6]


def test_high_round_breaks_the_streak():
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(7) is False  # breaks it
    assert tracker.process(4) is False  # restarts at 1
    assert tracker.process(5) is False
    assert tracker.process(6) is True


def test_none_total_breaks_the_streak():
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(None) is False
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True


def test_fire_then_skip_one_round_then_restart():
    tracker = PatternTracker()
    tracker.process(4)
    tracker.process(5)
    assert tracker.process(6) is True  # fires

    # the round just bet on is skipped, regardless of its own total
    assert tracker.process(9) is False

    # next round after the skip starts a fresh streak from 0
    assert tracker.process(3) is False
    assert tracker.process(3) is False
    assert tracker.process(3) is True
    assert tracker.last_streak_totals == [3, 3, 3]


def test_boundary_value_at_threshold_qualifies():
    tracker = PatternTracker(low_threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_boundary_value_above_threshold_does_not_qualify():
    tracker = PatternTracker(low_threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(7) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_streak_length_and_threshold_are_configurable():
    tracker = PatternTracker(low_threshold=5, streak_length=2)
    assert tracker.process(5) is False
    assert tracker.process(5) is True
    assert tracker.last_streak_totals == [5, 5]
```

- [ ] **Step 5: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.bettor.pattern'`

- [ ] **Step 6: Implement `PatternTracker`**

`services/bettor/pattern.py`:

```python
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
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pattern.py -v`
Expected: 7 passed

- [ ] **Step 8: Commit**

```bash
git add services/bettor/__init__.py services/bettor/pattern.py pytest.ini tests/__init__.py tests/test_pattern.py requirements.txt
git commit -m "$(cat <<'EOF'
Add PatternTracker: pure streak state machine for the 1st-half-over pattern

First unit-tested module in the repo — pure, no I/O, so no Redis/network
mocking needed. Covers fire-on-3, break-and-restart, fire-then-skip,
None-total handling, and threshold/streak-length configurability.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

---

## Task 4: `TargetTracker` (next-match resolution + stale-fire guard) + unit tests

**Files:**
- Create: `services/bettor/targeting.py`
- Test: `tests/test_targeting.py`

**Interfaces:**
- Consumes: `MatchDiscovered` from `shared/events.py` (Task 1, unchanged model).
- Produces: `TargetTracker` with `.on_discovered(event) -> MatchDiscovered | None`, `.on_started(match_id)`, `.on_half_time(match_id)`, `.on_finished(match_id)`, `.arm() -> MatchDiscovered | None`, `.is_stale(match_id) -> bool`, `.bet_targets: set[int]` — consumed by `services/bettor/main.py` (Task 6).

- [ ] **Step 1: Write the failing tests**

`tests/test_targeting.py`:

```python
from shared.events import MatchDiscovered
from services.bettor.targeting import TargetTracker


def _discovered(match_id: int, home: str = "A", away: str = "B") -> MatchDiscovered:
    return MatchDiscovered(
        match_id=match_id,
        league_name="FC 25. 3x3. Conference League",
        home=home,
        away=away,
        kickoff_ts=0,
        starting_in_label="Starting in 1 minute",
    )


def test_arm_returns_already_known_upcoming_match_immediately():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(1))
    target = tracker.arm()
    assert target is not None
    assert target.match_id == 1
    assert 1 in tracker.bet_targets


def test_arm_waits_for_discovery_when_next_match_not_yet_known():
    tracker = TargetTracker()
    target = tracker.arm()
    assert target is None  # nothing known yet — pending

    handed = tracker.on_discovered(_discovered(2))
    assert handed is not None
    assert handed.match_id == 2
    assert 2 in tracker.bet_targets


def test_discovery_without_a_pending_arm_does_not_hand_out_a_target():
    tracker = TargetTracker()
    handed = tracker.on_discovered(_discovered(3))
    assert handed is None
    assert tracker.bet_targets == set()


def test_same_match_is_never_targeted_twice():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(4))
    first = tracker.arm()
    assert first is not None

    # a second, unrelated fire while match 4 is still the latest-known
    # upcoming match must not re-target it
    second = tracker.arm()
    assert second is None


def test_is_stale_true_once_half_time_or_finished_seen():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(5))
    assert tracker.is_stale(5) is False

    tracker.on_started(5)
    assert tracker.is_stale(5) is False

    tracker.on_half_time(5)
    assert tracker.is_stale(5) is True


def test_is_stale_true_once_finished():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(6))
    tracker.on_finished(6)
    assert tracker.is_stale(6) is True


def test_is_stale_false_for_unknown_match():
    tracker = TargetTracker()
    assert tracker.is_stale(999) is False


def test_arm_skips_a_target_that_has_already_started():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(7))
    tracker.on_started(7)  # no longer "upcoming" by the time arm() is called
    target = tracker.arm()
    assert target is None  # falls back to waiting for the next discovery
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_targeting.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.bettor.targeting'`

- [ ] **Step 3: Implement `TargetTracker`**

`services/bettor/targeting.py`:

```python
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
        if it's already known, still upcoming, and not yet targeted;
        otherwise marks that a bet is owed to whichever match is
        discovered next."""
        latest = self._latest_discovered
        if (
            latest is not None
            and latest.match_id not in self.bet_targets
            and self._status.get(latest.match_id) == "upcoming"
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_targeting.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add services/bettor/targeting.py tests/test_targeting.py
git commit -m "$(cat <<'EOF'
Add TargetTracker: next-match resolution + stale-fire guard for the bettor

Pure state machine (no I/O) tracking the latest MatchDiscovered as the bet
target, handing it out either immediately on pattern-fire or on the next
discovery, and flagging a target as stale once its 1st half is already
known to be over.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

---

## Task 5: `BetExecutor` — API-based bet placement (reworked from the original UI-click plan)

**This task's approach changed mid-execution — the section below is the
rewritten version; see "Superseded plan" at the end for the original.**

While attempting the original UI-click design live (see below), it proved
too flaky to trust with real money: Vue-SPA re-render races on the "1st
half" tab click, plus `connect_over_cdp` hanging once ~10 orphaned tabs
from earlier exploration accumulated (fixed operationally by closing them
via `curl http://127.0.0.1:9222/json/close/<id>`, but not a good foundation
for unattended real-money placement). The user explicitly decided, live,
to switch to the same philosophy `services/collector/xbet_client.py`
already uses for reads: call the site's own JSON API directly instead of
driving the rendered page.

**Files:**
- Create: `services/bettor/betting_api.py` (replaces the planned `services/bettor/browser.py` — renamed since it no longer touches a browser page directly, only a lightweight CDP read for auth)
- Create: `tests/test_betting_api.py`
- Modify: `requirements.txt` (added `brotli` — the site's API responses are `Content-Encoding: br`)

**Interfaces:** (unchanged from the original plan, so Task 6 doesn't care which mechanism is behind it)
- `BetResult(success: bool, reason: str | None = None, odds: float | None = None)`
- `BetExecutor(api_base: str, cdp_url: str, timeout_seconds: float, auth_reader=None, transport=None)` — the last two are test-injection seams, not part of the production call shape.
- `async BetExecutor.place_bet(match_id: int, home: str, away: str, stake: float, line: float = 6.5) -> BetResult`

- [x] **Step 1: Capture the real request**

Done via a raw-CDP (not Playwright) passive network monitor watching an
already-open browser tab, while the user placed one real, authorized
capture bet (90 FCFA, "Total. 1st half", Over 6.5) manually in that same
tab. Two things had to be worked out first, both recorded in
`betting_api.py`'s module docstring and the design spec:

1. The first monitor design used a second, independently-connected
   Playwright `connect_over_cdp` session as the passive observer. Verified
   live (reproduced twice) that this silently receives **zero** Network
   events triggered by another session's activity on an already-open tab
   — even with a completely fresh attach and no intervening navigation.
   This is a real gap in "watch this tab from a second Playwright
   process," not a flake.
2. Fixed by dropping to raw CDP: a direct websocket connection to the
   tab's own `webSocketDebuggerUrl` (`Network.enable` + listening for
   `Network.requestWillBeSent`/`responseReceived`/`loadingFinished`, then
   `Network.getResponseBody`) does not have that gap — verified capturing
   both the page's own background polling and synthetic cross-session test
   POSTs (including their bodies) before trusting it for the real capture.

Captured the real placement call: `POST /service-api/LiveBet/Secure/MakeBetWeb`,
full request body, both auth headers, and the `Success: true` response
(bet id, new balance) — see `betting_api.py`'s docstring for the exact
shapes.

- [x] **Step 2: Identify the auth source**

Compared the captured `x-auth`/`x-hd` header values against the browser's
own cookies and `localStorage` (via a single, short-lived
`connect_over_cdp` read — cookies + one `page.evaluate`, no navigation):
`x-auth`'s JWT is byte-identical to the `access_token` cookie; `x-hd` is
byte-identical to the `.token` field inside `localStorage["fp_d"]`. Both
carry their own expiry landing within ~1 minute of each other (~4h window)
and are auto-refreshed by the page's own JS while the browser stays open —
so no fingerprint-generation logic needed, and no caching past that window
in `BetExecutor` either; it re-reads both fresh before every bet.

- [x] **Step 3: Implement `BetExecutor` against the API**

`services/bettor/betting_api.py` — `read_auth_from_browser()` does the
read-only CDP touch; `BetExecutor.place_bet()` fetches the current
coefficient from `GetGameZip` (filtered to `T=9` / `P=<line>` / `G=17`,
matching `xbet_client.py`'s documented codes — `G=17` confirmed 2026-09-10
to mean 1st-half totals, by matching a pre-match UI snapshot's Over/Under
6.5 prices coefficient-for-coefficient against the same match's raw feed;
see the code comment and ledger for the full cross-check), then POSTs to
`MakeBetWeb` with the fetched coefficient, `CheckCf: 2` (coefficient-
staleness tolerance — matches the verified working real request), and the
configured stake. No matching odds entry (market already closed) is
treated as the stale-fire guard. Every failure path returns
`BetResult(success=False, reason=...)` rather than raising, same contract
as originally planned.

- [x] **Step 4: Unit tests**

`tests/test_betting_api.py` — 7 tests, offline: `httpx.MockTransport`
stands in for the network, a fake `auth_reader` stands in for the browser
touch. Covers the success path (asserts the exact request body/headers
sent), auth-read failure (asserts zero network calls happen), stale-fire
guard (no matching market / wrong line), odds-lookup HTTP error,
server-rejected bet (`Success: false`), and a network error on the
placement call itself. Unlike the original UI-driven design (which the
spec called "not meaningfully unit-testable"), this mechanism is fully
testable without a real browser or network — run with
`.venv/bin/python -m pytest tests/test_betting_api.py -v`.

- [ ] **Step 5: Verify the replacement client end-to-end**

Not yet done. The Step-1 capture bet's authorization covered exactly that
one placement, used to observe the real request — it does **not** extend
to a second real bet placed *through this new client* to verify it works.
Ask the user again before placing one. If a real verification bet isn't
wanted yet, an acceptable weaker signal (per the design spec / CLAUDE.md's
handoff notes) is confirming the API distinguishes "bad request" from "bet
accepted" by deliberately sending a slightly-wrong payload (e.g. a stale
match id) and checking the error shape is legible — this was not done as
part of this task and remains open.

- [ ] **Step 6: Commit**

```bash
git add services/bettor/betting_api.py tests/test_betting_api.py requirements.txt \
        docs/superpowers/specs/2026-09-09-first-half-over-pattern-bettor-design.md \
        docs/superpowers/plans/2026-09-09-first-half-over-pattern-bettor.md
git commit -m "$(cat <<'EOF'
Add BetExecutor: API-based bet placement (reworked from UI-click design)

Places bets via a direct authenticated POST to the site's own
LiveBet/Secure/MakeBetWeb endpoint instead of driving the betting UI with
Playwright clicks -- the UI-click approach proved too flaky live to trust
with real money. The real request was reverse-engineered by capturing one
authorized real bet with a raw-CDP network monitor (a second Playwright
session watching the same tab was tried first and found to silently miss
cross-session network events). Auth (a bearer JWT plus a device-token
header) is read fresh from the browser's own cookies/localStorage before
every bet rather than cached, since both expire on a ~4h window.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01UcVB1h5PrwriZKvy6ALZjD
EOF
)"
```

---

<details>
<summary>Superseded plan: live selector reconnaissance + Playwright/CDP UI-click bet placement (not implemented — kept for history)</summary>

This site is a Vue SPA that re-renders unpredictably — verified live during design: the three period tabs ("Main game"/"1st half"/"2nd half") and the `Total. 1st half` market section with `Over 6.5`/`Under 6.5` rows were confirmed to exist with exactly that literal text, but the bet-slip's stake input and confirm button were never successfully reached in a live session during design (repeated navigation/render-timing flakiness). This task pins those two selectors down first, against the live site, then wires them straight into `BetExecutor` — nothing here is guessed.

**Files:**
- Create: `scratch/recon_bet_slip.py` (throwaway — deleted by the end of this task, never committed)
- Create: `services/bettor/browser.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `BetResult(success: bool, reason: str = "", odds: float | None = None)` and `BetExecutor(cdp_url: str)` with `async .start()`, `async .stop()`, `async .place_bet(match_id: int, home: str, away: str, stake: float, line: float) -> BetResult` — consumed by `services/bettor/main.py` (Task 6).

- [ ] **Step 1: Add Playwright to `requirements.txt` and install it**

Add a line to `requirements.txt`:

```
playwright>=1.62
```

Run: `.venv/bin/pip install -q -r requirements.txt` (the browser binaries themselves are not needed — `connect_over_cdp` attaches to the operator's already-running Chrome; do not run `playwright install`).

- [ ] **Step 2: Write and run the recon script against the live site**

`scratch/recon_bet_slip.py`:

```python
from playwright.sync_api import sync_playwright

LEAGUE_SLUG = "2860561-fc-25-3x3-conference-league"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()

    page.goto("https://1xbet.cm/en/esports/virtual/fifa", wait_until="domcontentloaded", timeout=30000)
    links = page.locator(f"a[href*='{LEAGUE_SLUG}'][href*='/live/']")
    links.first.wait_for(state="visible", timeout=20000)
    target = links.first.evaluate("e => e.href")
    print("target:", target)

    page.goto(target, wait_until="domcontentloaded", timeout=30000)

    # Render-ready signal: "Main game" is the always-present, unique-text
    # active tab label — wait for it before touching anything else.
    main_tab = page.get_by_text("Main game", exact=True)
    main_tab.wait_for(state="visible", timeout=20000)
    page.wait_for_timeout(1500)  # let the initial market list finish rendering

    # The site's Vue rendering is not reliably synchronous after a single
    # click — retry the tab click until the target heading actually shows.
    heading = page.get_by_text("Total. 1st half", exact=True)
    tab = page.get_by_text("1st half", exact=True)
    for attempt in range(8):
        tab.click(timeout=5000)
        try:
            heading.wait_for(state="visible", timeout=3000)
            print(f"1st half tab active after attempt {attempt + 1}")
            break
        except Exception:
            continue
    else:
        raise SystemExit("could not activate the 1st half tab — rerun, or pick a different live match")

    section = heading.locator("xpath=ancestor::*[.//text()[contains(.,'Over') and contains(.,'Under')]][1]")
    over_row = section.get_by_text("Over 6.5", exact=False).first
    over_row.wait_for(state="visible", timeout=10000)
    print("clicking:", over_row.inner_text())
    over_row.click(timeout=10000)
    page.wait_for_timeout(2000)

    page.screenshot(path="scratch/recon_slip.png")

    inputs = page.eval_on_selector_all(
        "input",
        "els => els.map(e => ({type:e.type, cls:e.className, placeholder:e.placeholder, name:e.name})).filter(x => x.type !== 'hidden' && x.type !== 'radio')"
    )
    print("VISIBLE INPUTS:", inputs)

    buttons = page.eval_on_selector_all(
        "button",
        "els => els.map(e => ({cls:e.className, text:(e.innerText||'').trim()})).filter(x => x.text.length > 0 && x.text.length < 40)"
    )
    print("BUTTONS WITH TEXT:", buttons)

    page.close()
```

Run: `.venv/bin/python scratch/recon_bet_slip.py`

If the tab-activation loop exhausts all 8 attempts, or the target match finishes mid-script (these are short virtual matches — this can happen), just rerun it; it re-picks whatever is currently live each time.

- [ ] **Step 3: Identify the stake input and confirm-bet button from the output**

Read the printed `VISIBLE INPUTS` and `BUTTONS WITH TEXT` lists. Identify: (a) the stake/sum input — the one that's part of the bet-slip panel (right-hand side of the page, near the odds you just clicked, not the site's own search box or login fields), and (b) the button whose visible text is the "place/confirm this bet" action. Open `scratch/recon_slip.png` to visually confirm which input/button they are if the printed class names are ambiguous (Vue scoped-CSS classes are typically hashed and non-descriptive, e.g. `data-v-xxxxx`).

Write down the two Playwright selectors you'll use for them (e.g. a CSS class selector, or `get_by_placeholder(...)`/`get_by_role("button", name=...)` if that's more robust than a hashed class name) — they go directly into Step 5 below.

- [ ] **Step 4: Delete the recon script**

```bash
rm -f scratch/recon_bet_slip.py scratch/recon_slip.png
rmdir scratch 2>/dev/null || true
```

It was throwaway reconnaissance, not part of the shipped service — do not leave it in the repo or commit it.

- [ ] **Step 5: Implement `BetExecutor`, using the selectors found in Step 3**

`services/bettor/browser.py` — replace `STAKE_INPUT_SELECTOR` and `PLACE_BET_BUTTON_SELECTOR` below with the exact values identified in Step 3 before moving on; the module must not ship with anything other than a real, working selector in those two constants:

```python
"""BetExecutor — drives the operator's already-logged-in Chrome (over
CDP) to place a real bet on 1xbet's "Total. 1st half" market.

Connects once per service lifetime (see main.py's start()/stop()) and
keeps one dedicated tab open for the whole run, separate from whatever
tabs the operator has open themselves. Never launches a new browser and
never logs in — see the CDP_URL constraint in the design doc.

Every step is wrapped so a selector miss, timeout, or site-side rejection
(odds changed, insufficient funds, market suspended) returns a
BetResult(success=False, reason=...) instead of raising into the
service's event loop — one bad UI interaction must not crash the bettor.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from playwright.async_api import Page, Playwright, async_playwright

LEAGUE_LISTING_URL = "https://1xbet.cm/en/esports/virtual/fifa"
LEAGUE_SLUG = "2860561-fc-25-3x3-conference-league"

# How long to keep re-checking the live match list for a target that's
# still "upcoming" at the moment the bet is attempted — these are short
# virtual matches, so a target usually goes live within this window.
LIVE_LOOKUP_TIMEOUT_SECONDS = 30.0
LIVE_LOOKUP_POLL_SECONDS = 1.5

UI_ACTION_TIMEOUT_MS = 15_000
TAB_ACTIVATION_ATTEMPTS = 8

# Found by live reconnaissance against the bet slip (see this task's Step
# 2-3 in docs/superpowers/plans/2026-09-09-first-half-over-pattern-bettor.md).
STAKE_INPUT_SELECTOR = "<selector identified in Step 3>"
PLACE_BET_BUTTON_SELECTOR = "<selector identified in Step 3>"


@dataclass
class BetResult:
    success: bool
    reason: str = ""
    odds: float | None = None


class BetExecutor:
    def __init__(self, cdp_url: str):
        self._cdp_url = cdp_url
        self._playwright: Playwright | None = None
        self._page: Page | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
        context = browser.contexts[0]
        self._page = await context.new_page()

    async def stop(self) -> None:
        if self._page is not None:
            await self._page.close()
        if self._playwright is not None:
            await self._playwright.stop()

    async def place_bet(self, match_id: int, home: str, away: str, stake: float, line: float) -> BetResult:
        assert self._page is not None, "BetExecutor.start() not called"
        page = self._page

        match_url = await self._find_live_match_url(match_id)
        if match_url is None:
            return BetResult(
                success=False,
                reason=f"match {match_id} ({home} vs {away}) never appeared live within {LIVE_LOOKUP_TIMEOUT_SECONDS:.0f}s",
            )

        try:
            await page.goto(match_url, wait_until="domcontentloaded", timeout=UI_ACTION_TIMEOUT_MS)

            main_tab = page.get_by_text("Main game", exact=True)
            await main_tab.wait_for(state="visible", timeout=UI_ACTION_TIMEOUT_MS)
            await page.wait_for_timeout(1500)  # let the initial market list finish rendering

            heading = page.get_by_text("Total. 1st half", exact=True)
            tab = page.get_by_text("1st half", exact=True)
            activated = False
            for _ in range(TAB_ACTIVATION_ATTEMPTS):
                await tab.click(timeout=5_000)
                try:
                    await heading.wait_for(state="visible", timeout=3_000)
                    activated = True
                    break
                except Exception:  # noqa: BLE001 — expected on a slow render, just retry the click
                    continue
            if not activated:
                return BetResult(success=False, reason="could not activate the '1st half' tab")

            line_label = f"Over {line:g}"
            section = heading.locator(
                "xpath=ancestor::*[.//text()[contains(.,'Over') and contains(.,'Under')]][1]"
            )
            odd_row = section.get_by_text(line_label, exact=False).first
            await odd_row.wait_for(state="visible", timeout=UI_ACTION_TIMEOUT_MS)
            odds_text = (await odd_row.inner_text()).strip()
            await odd_row.click(timeout=UI_ACTION_TIMEOUT_MS)

            stake_input = page.locator(STAKE_INPUT_SELECTOR).first
            await stake_input.wait_for(state="visible", timeout=UI_ACTION_TIMEOUT_MS)
            await stake_input.fill(str(stake))

            place_button = page.locator(PLACE_BET_BUTTON_SELECTOR).first
            await place_button.wait_for(state="visible", timeout=UI_ACTION_TIMEOUT_MS)
            await place_button.click(timeout=UI_ACTION_TIMEOUT_MS)

            return BetResult(success=True, odds=_parse_odds(odds_text))
        except Exception as err:  # noqa: BLE001 — any UI miss must surface as a clean failure, not crash the service
            return BetResult(success=False, reason=str(err))

    async def _find_live_match_url(self, match_id: int) -> str | None:
        assert self._page is not None
        deadline = time.monotonic() + LIVE_LOOKUP_TIMEOUT_SECONDS
        selector = f"a[href*='{LEAGUE_SLUG}'][href*='/live/{match_id}-']"
        while time.monotonic() < deadline:
            await self._page.goto(LEAGUE_LISTING_URL, wait_until="domcontentloaded", timeout=UI_ACTION_TIMEOUT_MS)
            link = self._page.locator(selector).first
            try:
                href = await link.evaluate("e => e.href", timeout=2_000)
                if href:
                    return href
            except Exception:  # noqa: BLE001 — not found yet, keep polling
                pass
            await asyncio.sleep(LIVE_LOOKUP_POLL_SECONDS)
        return None


def _parse_odds(text: str) -> float | None:
    match = re.search(r"(\d+\.\d+)", text)
    return float(match.group(1)) if match else None
```

- [ ] **Step 6: Manually verify against the live site**

Run a throwaway script (same shape as Step 2's, or a short `python -c`) that imports `BetExecutor`, calls `start()`, then `place_bet()` against a currently-live 3x3 match with a small stake — watch the actual Chrome window to confirm it navigates, switches to 1st half, clicks the correct Over line, fills the stake, and clicks confirm. This is real money — do this once, watched, before Task 9's full end-to-end trial relies on it unattended.

- [ ] **Step 7: Commit**

```bash
git add services/bettor/browser.py requirements.txt
git commit -m "$(cat <<'EOF'
Add BetExecutor: CDP-driven bet placement on Total. 1st half

Connects to the operator's already-logged-in Chrome via connect_over_cdp,
navigates to the target match, activates the 1st half tab with
verified retries (the site's Vue rendering is not reliably synchronous
after a single click), and places the configured stake on the Over line.
Stake-input/confirm-button selectors were found by live reconnaissance
against the real bet slip, not guessed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

</details>

---

## Task 6: `services/bettor/main.py` — wiring

**Files:**
- Create: `services/bettor/main.py`

**Interfaces:**
- Consumes: `PatternTracker` (Task 3), `TargetTracker` (Task 4), `BetExecutor`/`BetResult` (Task 5), `EventBus`/`load_config`/`get_logger` (existing `shared/`), `MatchDiscovered`/`MatchStarted`/`MatchHalfTime`/`MatchFinished`/`PatternArmed`/`BetPlaced`/`BetFailed`/`BetSettled` (Task 1).
- Produces: `python -m services.bettor.main` entrypoint, publishing `PatternArmed`/`BetPlaced`/`BetFailed`/`BetSettled` onto `config.channel_match_events` — consumed by `services/display` (Task 7) and `run.sh` (Task 8).

- [ ] **Step 1: Implement `main.py`**

`services/bettor/main.py`:

```python
"""bettor — Pattern 1: "1st Half Over 6.5" streak detector + live bet
placement.

Subscribes to xbet.match_events like display does, feeds every finished
round's 1st-half total into PatternTracker, resolves the bet target via
TargetTracker, and drives BetExecutor to actually place it. Publishes
PatternArmed/BetPlaced/BetFailed/BetSettled back onto the same channel —
just another producer on the existing bus, same shape as the aggregator.

Run standalone:
    python -m services.bettor.main
"""
from __future__ import annotations

import asyncio
import signal

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
)
from shared.logging import get_logger


def _first_half_total(event: MatchFinished) -> int | None:
    if event.first_half is None:
        return None
    return event.first_half.home_goals + event.first_half.away_goals


async def run() -> None:
    config = load_config()
    log = get_logger("bettor", config.log_dir)

    bus = EventBus(config.redis_url)
    await bus.connect()

    tracker = PatternTracker(low_threshold=config.pattern_low_threshold, streak_length=config.pattern_streak_length)
    targets = TargetTracker()
    executor = BetExecutor(config.api_base, config.cdp_url, config.http_timeout_seconds)

    log.info(
        f"starting — streak_length={config.pattern_streak_length} "
        f"low_threshold={config.pattern_low_threshold} bet_line={config.pattern_bet_line} "
        f"stake={config.bet_stake_amount} cdp_url={config.cdp_url}"
    )

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

    async def consume() -> None:
        async for event in bus.subscribe_match_events(config.channel_match_events):
            try:
                if isinstance(event, MatchDiscovered):
                    target = targets.on_discovered(event)
                    if target is not None:
                        await place(target)
                elif isinstance(event, MatchStarted):
                    targets.on_started(event.match_id)
                elif isinstance(event, MatchHalfTime):
                    targets.on_half_time(event.match_id)
                elif isinstance(event, MatchFinished):
                    targets.on_finished(event.match_id)

                    if event.match_id in targets.bet_targets and event.first_half is not None:
                        first_half_total = event.first_half.home_goals + event.first_half.away_goals
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

                    fired = tracker.process(_first_half_total(event))
                    if fired:
                        log.info(f"PATTERN ARMED — streak {tracker.last_streak_totals}")
                        await bus.publish(
                            config.channel_match_events,
                            PatternArmed(
                                pattern_name="1st_half_over_6.5_streak",
                                qualifying_totals=list(tracker.last_streak_totals),
                            ),
                        )
                        target = targets.arm()
                        if target is not None:
                            await place(target)
            except Exception as err:  # noqa: BLE001 — one bad event must not kill the subscription
                log.error(f"failed to process {event.kind}: {err}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    consumer_task = asyncio.create_task(consume())
    await stop.wait()

    log.info("shutting down...")
    consumer_task.cancel()
    await executor.aclose()
    await bus.close()


if __name__ == "__main__":
    asyncio.run(run())
```

- [ ] **Step 2: Smoke-test the module imports and constructs cleanly**

Run: `.venv/bin/python -c "import services.bettor.main"`
Expected: no exception (module-level code only defines functions; nothing runs at import time).

- [ ] **Step 3: Commit**

```bash
git add services/bettor/main.py
git commit -m "$(cat <<'EOF'
Wire up services/bettor: subscribe, detect, target, bet, publish

Same shape as aggregator/display's main.py — asyncio consume loop with
SIGINT/SIGTERM shutdown. Feeds finished-round totals into PatternTracker,
resolves the next-round target via TargetTracker (with the stale-fire
guard applied immediately before every bet attempt), and drives
BetExecutor, publishing PatternArmed/BetPlaced/BetFailed/BetSettled.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

---

## Task 7: Display integration — render the new events + `data/bets.log`

**Files:**
- Modify: `services/display/render.py`
- Modify: `services/display/main.py`

**Interfaces:**
- Consumes: `PatternArmed`/`BetPlaced`/`BetFailed`/`BetSettled` (Task 1), `config.bets_log_path` (Task 2).
- Produces: `render_pattern_armed`, `render_bet_placed`, `render_bet_failed`, `render_bet_settled`, `log_bet_event` in `render.py` — used only by `display/main.py`.

- [ ] **Step 1: Add render functions to `services/display/render.py`**

Add `import time` next to the existing `from datetime import datetime` line at the top of the file.

Extend the existing `from shared.events import (...)` block to also import:

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
)
```

Add near the bottom, before `render_legend()`:

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


BettorEvent = PatternArmed | BetPlaced | BetFailed | BetSettled


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

- [ ] **Step 2: Wire the new events into `services/display/main.py`**

Update the imports:

```python
from services.display.render import (
    log_bet_event,
    log_finished,
    render_backfill_header,
    render_bet_failed,
    render_bet_placed,
    render_bet_settled,
    render_discovered,
    render_finished,
    render_half_time,
    render_legend,
    render_live_header,
    render_live_score,
    render_pattern_armed,
    render_started,
)
from shared.events import (
    BetFailed,
    BetPlaced,
    BetSettled,
    MatchDiscovered,
    MatchFinished,
    MatchHalfTime,
    MatchScoreChanged,
    MatchStarted,
    PatternArmed,
)
```

Open a second log file handle right after the existing `result_log_file`/`result_log_console` setup:

```python
    config.bets_log_path.parent.mkdir(parents=True, exist_ok=True)
    bets_log_file = config.bets_log_path.open("a", encoding="utf-8")
    bets_log_console = Console(file=bets_log_file, no_color=True, width=RESULT_LOG_WIDTH, highlight=False)
```

Extend the `consume()` branch chain:

```python
                elif isinstance(event, MatchFinished):
                    render_finished(event)
                    log_finished(event, result_log_console)
                    result_log_file.flush()
                elif isinstance(event, PatternArmed):
                    render_pattern_armed(event)
                    log_bet_event(event, bets_log_console)
                    bets_log_file.flush()
                elif isinstance(event, BetPlaced):
                    render_bet_placed(event)
                    log_bet_event(event, bets_log_console)
                    bets_log_file.flush()
                elif isinstance(event, BetFailed):
                    render_bet_failed(event)
                    log_bet_event(event, bets_log_console)
                    bets_log_file.flush()
                elif isinstance(event, BetSettled):
                    render_bet_settled(event)
                    log_bet_event(event, bets_log_console)
                    bets_log_file.flush()
```

And close the new file handle in shutdown, next to the existing `result_log_file.close()`:

```python
    result_log_file.close()
    bets_log_file.close()
```

- [ ] **Step 3: Verify the module still imports cleanly**

Run: `.venv/bin/python -c "import services.display.main"`
Expected: no exception.

- [ ] **Step 4: Commit**

```bash
git add services/display/render.py services/display/main.py
git commit -m "$(cat <<'EOF'
Render the betting-pattern events and log them to data/bets.log

PatternArmed/BetPlaced/BetFailed/BetSettled get their own terminal panels,
same visual language as the existing RESULT grid, plus a plain-text audit
trail in data/bets.log mirroring how data/result.log already works.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

---

## Task 8: `run.sh` orchestration + `README.md`

**Files:**
- Modify: `run.sh`
- Modify: `README.md`
- Modify: `.gitignore` (track `data/bets.log` like `data/result.log`)

**Interfaces:**
- Produces: `./run.sh start|stop|status|restart` managing `bettor` alongside `collector`/`aggregator`.

- [ ] **Step 1: Add `bettor` to `run.sh`**

In the `start)` case, add after `start_bg aggregator services.aggregator.main`:

```bash
    start_bg bettor services.bettor.main
```

In `status()`, change the loop:

```bash
    for name in collector aggregator bettor; do
```

Replace the `stop)` case with:

```bash
stop)
    stop_bg collector
    stop_bg aggregator
    stop_bg bettor
    echo "[stop] redis-server left running (shared resource) — 'redis-cli shutdown' to stop it too"
    ;;
```

- [ ] **Step 2: Track `data/bets.log` in git**

Check the current `.gitignore` exception for `data/result.log` (likely `data/*` ignored with a `!data/result.log` exception) and add the same for `data/bets.log`:

```bash
!data/bets.log
```

- [ ] **Step 3: Verify `./run.sh status` reports the new service**

Run: `./run.sh status`
Expected: prints `bettor: stopped` (or `running` if already started) alongside `collector`/`aggregator`/`redis` lines.

- [ ] **Step 4: Document Pattern 1 in `README.md`**

Add a new `## Betting patterns` section (link it from the table of contents near the top, after "Event catalogue"):

```markdown
## Betting patterns

### Pattern 1 — "1st Half Over 6.5" streak

`services/bettor/` (see `services/bettor/pattern.py` for the exact state
machine) watches every finished round's 1st-half combined goal total. 3
consecutive rounds at or under `PATTERN_LOW_THRESHOLD` (default 6) fire a
real bet — `BET_STAKE_AMOUNT` (default 90, FCFA) on the *next* round's
`Total. 1st half` market, `Over PATTERN_BET_LINE` (default 6.5). After a
fire, the streak resets and the very next round is excluded from
counting (it's the one just bet on) — the round after that restarts the
count from 0.

Bets are placed live, from the very first run — there is no dry-run
mode. The bot drives the operator's already-logged-in Chrome over the
Chrome DevTools Protocol (`CDP_URL`, default `http://127.0.0.1:9222`) —
it never launches its own browser or logs in itself. Unlike the JSON-API
data path described in [How data is sourced](#how-data-is-sourced), this
**does** need a real, unblocked browser session, because it has to
interact with the actual betting UI, not just read data.

If the target round's 1st half is already over (half-time or finished)
by the moment the bot is actually ready to click — real wall-clock time
passes during navigation — the bet is skipped and logged as failed
rather than clicked into a stale market.

Every pattern fire / bet placed / bet failed / bet settled is rendered
in the terminal (same panel style as the RESULT grid) and appended to
`data/bets.log` (plain text, tracked in git, same convention as
`data/result.log`) so the pattern's real hit-rate is reviewable over
time.

Config knobs: `BET_STAKE_AMOUNT`, `PATTERN_STREAK_LENGTH`,
`PATTERN_LOW_THRESHOLD`, `PATTERN_BET_LINE`, `CDP_URL`, `BETS_LOG_PATH` —
see `.env.example`.
```

Add the four new event kinds to the existing "Event catalogue" table (same format as the five rows already there):

```markdown
| `xbet.match_events` | `PatternArmed` (`pattern_armed`) | A betting pattern's trigger condition is met. | `pattern_name`, `qualifying_totals` |
| `xbet.match_events` | `BetPlaced` (`bet_placed`) | A bet was successfully placed. | `match_id`, `stake`, `line`, `odds` |
| `xbet.match_events` | `BetFailed` (`bet_failed`) | A bet was skipped or failed. | `match_id?`, `reason` |
| `xbet.match_events` | `BetSettled` (`bet_settled`) | The bet's target match finished. | `match_id`, `won`, `first_half_total` |
```

Update the Roadmap section to remove the now-implemented "a betting service subscribing to xbet.match_events... is the natural next addition" bullet (or mark it done and point at the new "Betting patterns" section).

- [ ] **Step 5: Commit**

```bash
git add run.sh README.md .gitignore
git commit -m "$(cat <<'EOF'
Wire the bettor service into run.sh and document Pattern 1 in README

./run.sh now starts/stops/status-checks bettor alongside collector and
aggregator. README documents the pattern's exact trigger logic, config
knobs, and the new event kinds/data/bets.log audit trail.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LVnvSCgS5S11Bht9aZPxZG
EOF
)"
```

---

## Task 9: End-to-end live trial

Not a code change — the final verification step before leaving the bot running unattended, since Task 5's Step 6 only verified `BetExecutor` in isolation, not the full pattern-detection-to-bet pipeline.

**Files:** none

- [ ] **Step 1: Run the full stack**

```bash
./run.sh restart
```

Watch the terminal (which attaches `display`) and `logs/bettor.log` simultaneously (`tail -f logs/bettor.log` in a second terminal) for at least 3-4 finished rounds.

- [ ] **Step 2: Confirm the streak counts correctly against real rounds**

Cross-check `logs/bettor.log`'s implicit streak state against the actual 1st-half totals printed in the terminal's RESULT panels (or `data/result.log`) for the same rounds — the streak should visibly increment 1→2→3 across consecutive ≤6 rounds and reset to 0 on any ≥7 round, matching `PatternTracker`'s tested behavior from Task 3.

- [ ] **Step 3: If a real fire happens during the trial, watch the bet go through**

Confirm in the terminal: a `PATTERN FIRED` panel, then either a `BET PLACED` panel (verify manually, in the browser, that the bet actually appears in "My bets" on 1xbet with the correct stake/line/match) or a `BET FAILED` panel with a legible reason. Confirm `data/bets.log` gained the matching entry.

- [ ] **Step 4: Confirm settlement**

Once the target match finishes, confirm a `SETTLED` panel appears with the correct `WON`/`LOST` label matching the match's actual 1st-half total vs. `PATTERN_BET_LINE`.

- [ ] **Step 5: Leave it running, or stop it**

If everything in Steps 1-4 checked out, the bot can be left running (`./run.sh` already backgrounds `collector`/`aggregator`/`bettor` and only `display` is attached to the terminal — Ctrl+C stops watching, not the bot). Otherwise, `./run.sh stop` and go back to whichever task's behavior didn't match expectations.
