# Pattern 4: "Main Game Under 16.5" pair-streak — design

## Problem

Patterns 1–3 are implemented, live, and have proven the mechanism (streak
detection → target resolution → real-money bet placement via the site's own
JSON API). This adds a fourth, independent pattern on the same
`services/bettor/` process, with two genuinely new pieces the first three
didn't need:

1. A different streak shape — not "N consecutive qualifying rounds," but "of
   4 raw values across a fixed 2-round window, at least 3 qualify."
2. A different bet-placement policy — wait for the market's live odds to
   clear a minimum before submitting, rather than placing at whatever price
   is quoted the instant the pattern fires.

Both are additive extensions of existing pieces, not forks — same treatment
already given to `PatternTracker` (`direction`), `TargetTracker`
(`stale_statuses`), and `BetExecutor` (`period`/`over`/`bet_type`/`group`) in
the Pattern 2 and Pattern 3 specs.

## Pattern definition

For every finished round, take its two combined goal totals: 1st-half total
(`first_half.home_goals + first_half.away_goals`) and 2nd-half total
(`second_half.home_goals + second_half.away_goals`) — 2 values per round.

Group rounds into **non-overlapping pairs of 2** (4 values total per pair):

- If **≥ 3 of those 4 values are ≥ 9** → the pattern fires: bet on the
  **next** round's **Main Game → Total → Under 16.5** market.
- Otherwise (≤ 2 of 4 qualify) → no fire.
- Whether it fires or not, the pair buffer clears after the 2nd round is
  evaluated, and a fresh pair starts counting from the very next round.
- After a fire, the round immediately following is additionally **skipped
  entirely** (not counted as either round of the next pair) before counting
  resumes from zero.

Worked check against the reference example (Red Bull v West Ham: 1H=10,
2H=8; Braga v Gladbach: 1H=10, 2H=12): values `[10, 8, 10, 12]` → 3 of 4 are
≥ 9 (all but the `8`) → fires. The next round (Anderlecht v Chelsea)
finished at a combined total of 11, consistent with the pattern's
"next round's final total likely ≤ 16" premise. A round where *both* its
values are < 9 costs 2 of the 4 slots at once, which is why "one round
entirely under 9" alone is enough to prevent a fire (2 remaining qualifying
values from the other round can't reach the 3-of-4 bar) while "one round
with only one value under 9" still clears it (3 of 4 still qualify) — both
directly match the two worked cases you described.

## Architecture

No new service. Pattern 4 is a fourth, mostly-parallel handler set inside
`services/bettor/main.py`'s existing `consume()` loop, with one structural
difference from Patterns 1–3 (see "Async placement" below).

```
                         xbet.match_events
                                │
                 ┌──────────────┼──────────────────────┐
                 ▼              ▼                       ▼
           aggregator      display                  bettor
         (unchanged)   (+ pattern4 panels)   consume() loop:
                                               ├─ Pattern 1 handlers (sync place)
                                               ├─ Pattern 2 handlers (sync place)
                                               ├─ Pattern 3 handlers (sync place)
                                               └─ Pattern 4 handlers
                                                   (tracker4, targets4,
                                                    place() spawned as a task —
                                                    can block for the whole match)
                                               all → shared BetExecutor
```

### `services/bettor/pattern.py` — new `RoundPairStreakTracker`, `PatternTracker` untouched

`PatternTracker`'s life-cycle shape (fire → skip-one-round → reset) doesn't
fit this rule as a parameterization: `PatternTracker.process()` takes one
scalar and asks "does it individually qualify," accumulating a streak of
*consecutive* qualifying rounds. Pattern 4 instead needs to buffer exactly 2
rounds' worth of *raw values* and evaluate a *count threshold* across all 4
at once — a different shape, not a different `direction`. Forcing it into
`PatternTracker` (e.g. by inventing a synthetic per-round "qualifies"
boolean) would hide the actual rule behind a lossy reduction. New, separate,
equally small dataclass in the same file:

```python
@dataclass
class RoundPairStreakTracker:
    """Fires when >= required_count of the 4 (1st-half-total, 2nd-half-total)
    values across 2 consecutive finished rounds are >= half_threshold.
    Non-overlapping pairs: after 2 rounds are evaluated (fire or not), the
    pair buffer clears and a fresh pair starts counting from the very next
    round. On fire, the round immediately following is skipped entirely
    (not added to the next pair) before counting resumes. A round with
    either half's total unknown (None) resets the pair buffer without
    counting, same conservative stance PatternTracker takes for a None
    total."""

    half_threshold: int = 9
    required_count: int = 3

    _pending: list[tuple[int, int]] = field(default_factory=list, init=False, repr=False)
    _skip_next: bool = field(default=False, init=False, repr=False)
    last_pair_values: list[int] = field(default_factory=list, init=False)
    last_qualifying_count: int = field(default=0, init=False)
    last_outcome: Literal["counting", "reset", "skipped", "armed"] | None = field(default=None, init=False)

    @property
    def rounds_in_pair(self) -> int:
        """0, 1, or 2 (2 only transiently, cleared same call it's reached)."""
        return len(self._pending)

    def process(self, first_half_total: int | None, second_half_total: int | None) -> bool:
        """Feed one more finished round's two half-totals, in finish order.
        Returns True the moment this round completes a pair that meets
        required_count — the caller should bet on the *next* round when
        this returns True."""
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

`half_threshold`/`required_count` are hardcoded at the `main.py` call site
(`RoundPairStreakTracker(half_threshold=9, required_count=3)`) rather than
exposed as env vars — explicit decision: this is one precisely-defined rule,
not a family of tunable variants like Pattern 1/2's threshold+streak_length.

### Trigger point: `MatchFinished`, not `MatchHalfTime`

Unlike Pattern 1/3 (which deliberately evaluate at `MatchHalfTime` to avoid
losing the "next match" target to real-time slippage), Pattern 4 needs
*both* halves of a round before it can feed `RoundPairStreakTracker`, so it
must wait for `MatchFinished` — the same event Pattern 2 already triggers
on, for the identical reason (its own rule needs `second_half`). Both
`first_half` and `second_half` totals are read off the same `MatchFinished`
event:

```python
first_half_total = (
    None if event.first_half is None
    else event.first_half.home_goals + event.first_half.away_goals
)
second_half_total = (
    None if event.second_half is None
    else event.second_half.home_goals + event.second_half.away_goals
)
```

(`MatchFinished.first_half`/`second_half` are both typed `HalfScore | None`
— Pattern 2's spec already flagged this optionality for `second_half` and
deferred confirming it's reliably non-`None` in practice to the
implementation plan; this spec inherits the same open item for
`first_half`, and applies the same defensive `None` handling either way.)

### `services/bettor/betting_api.py` — `BetExecutor.place_bet()` extended, not forked

Two additive changes, both fully backward-compatible with Patterns 1–3's
existing call sites.

**1. Main-game period.** Add `MAIN_GAME_ID_OFFSET = 0` alongside the
existing offset constants (the module's own docstring already documents
that the raw `match_id` *is* the whole-match/"Main game" sub-game id — this
just gives it a name and a first real caller). Widen the signature:

```python
period: Literal[0, 1, 2] = 1
...
if period == 0:
    offset = MAIN_GAME_ID_OFFSET
elif period == 1:
    offset = FIRST_HALF_ID_OFFSET
else:
    offset = SECOND_HALF_ID_OFFSET
game_id = match_id + offset
```

(Replaces the current binary `offset = FIRST_HALF_ID_OFFSET if period == 1
else SECOND_HALF_ID_OFFSET` — behavior for `period=1`/`period=2` is
unchanged, `period=0` is new.)

**2. Odds-gated placement.** Add three new optional parameters:

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
    ...
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
    # ... existing POST body construction using `coef`/`auth`, unchanged from here
```

- `min_odds=None` (the default, and Patterns 1–3's implicit usage) hits the
  `if min_odds is None: return ...` branch on the very first iteration
  whenever `coef` isn't already acceptable — byte-identical to today's
  single-shot behavior. Patterns 1–3 need zero changes to their call sites.
- When `min_odds` is set, a `None` coefficient (market not currently quoted)
  and a below-threshold coefficient are treated the same way — both are
  "not ready yet," not "stop" — matching the accepted answer that this wait
  has no timeout of its own beyond the round itself becoming unbettable.
- The odds check and the submission read `coef` from the same iteration —
  no re-fetch between "odds look acceptable" and "send the bet," so there's
  no window where a last-second odds drop goes unnoticed between check and
  submit.
- **Auth is read at its current position, before the odds check/loop —
  deliberately not moved.** An earlier draft of this spec considered
  reading auth *after* the wait resolves (reasoning that a Pattern-4 wait
  can span several minutes, so freshest-possible tokens seemed safer).
  That would have been wrong: it would run the `GetGameZip` odds lookup
  before ever checking auth, breaking the existing guarantee (relied on by
  `tests/test_betting_api.py::test_auth_failure_does_not_hit_the_network`)
  that an auth failure short-circuits before any HTTP call — for Patterns
  1–3 too, not just Pattern 4. Since the auth/device tokens are valid for
  roughly 4 hours (per this module's own docstring) and a match round is a
  few minutes at most, reading auth once at the top and using it after an
  in-loop wait carries no real staleness risk — so the simplest option
  (leave it exactly where it is today) is also the correct one.

### `services/bettor/targeting.py` — reused unmodified

`TargetTracker(stale_statuses={"finished"})` — pregame and live are both
fair game for this market ("place on the next round regardless of whether
it's upcoming or live," per the pattern's own definition); only `"finished"`
closes it. Identical parameterization Pattern 2 already uses, for the
identical reason.

**No mutual exclusion, either direction.** Per explicit decision: Pattern 4
targets a market (`Main Game Under 16.5`) none of Patterns 1–3 touch, so
there's no shared-risk reason to block it, and it doesn't block them either.
`place()`'s `other_patterns` argument is `[]` for Pattern 4's calls; Pattern
1/2/3's own `other_patterns` lists are **not** extended to include
`targets4`.

### Async placement — the one real structural difference from Patterns 1–3

`consume()` processes one event at a time; Patterns 1–3's `place()` calls
are all `await`ed inline because `place_bet()` resolves in well under a
second. Pattern 4's `place_bet()` can now block for however long the round
takes to either clear 1.5 or finish — awaiting it inline would stall *every
other pattern's* event processing for that entire window. Fix: Pattern 4's
`place(...)` calls are wrapped in `asyncio.create_task(...)` instead of
`await`ed, at both call sites that can trigger it (the `arm()`-on-fire path
and the `on_discovered()`-pending-resolution path, mirroring exactly where
Patterns 1–3 call `place()` today).

Two small, necessary additions that come with this (not scope creep — both
are asyncio correctness requirements, not extra features):

- Keep a `list[asyncio.Task]` reference to each spawned task
  (`pattern4_tasks`) — an unreferenced `asyncio.Task` can be garbage
  collected mid-flight, silently abandoning the bet attempt.
- Attach a `done_callback` that logs any exception the task raised — a
  bare fire-and-forget task otherwise swallows an unexpected failure
  silently instead of surfacing it the way every other pattern's failures
  already surface via `BetFailed`/log lines.

At shutdown, these tasks are cancelled (best-effort) the same way
`consumer_task`/`watchdog_task` already are, without awaiting the
cancellation — consistent with, not a new instance of, the project's
already-documented pre-existing gap there.

### `services/bettor/main.py` — fourth handler set + module-level constants

```python
PATTERN4_NAME = "main_game_under_16.5_streak"
PATTERN4_MIN_ODDS = 1.5
PATTERN4_ODDS_POLL_INTERVAL_SECONDS = 5.0
```

(`MIN_ODDS`/poll interval are plain constants, not env vars — same
hardcode-the-rule-specific-numbers decision as the tracker's thresholds.)

```python
tracker4 = RoundPairStreakTracker(half_threshold=9, required_count=3)
targets4 = TargetTracker(stale_statuses={"finished"})
placed_matches4: set[int] = set()
pattern4_tasks: list[asyncio.Task] = []

def _log_pattern4_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(f"pattern4 placement task failed: {exc}")
```

`_market_label` widens from its current binary `1st half`/`2nd half` to a
3-way mapping including `period=0 → "Main game"`, so
`_market_label(0, False, config.pattern4_bet_line)` produces
`"Total. Main game Under 16.5"`. A new `_pair_condition_label()` helper
(mirroring Pattern 3's own dedicated `_winner_condition_label()`, since this
rule doesn't fit the generic `_condition_label`'s under/over-threshold
phrasing) produces e.g. `"2-round pair with >=3 of 4 half-totals >= 9"`.

**`MatchDiscovered` branch:** add `targets4.on_discovered(event)`; if it
resolves a target *and* `config.pattern4_enabled`, spawn (don't await)
`place(...)` via `asyncio.create_task`, append to `pattern4_tasks`, attach
the done-callback — same combined `is not None and config.pattern4_enabled`
single condition Pattern 2/3 already use at this exact call site:

```python
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
```

(Identical `place_call` shape to the `MatchFinished`-triggered path below —
duplicated here rather than factored out, matching how Patterns 1–3 already
duplicate their own `place_call` lambda between their `MatchDiscovered` and
fire-triggered call sites today.)

**`MatchStarted`/`MatchHalfTime` branches:** add `targets4.on_started(...)`
/ `targets4.on_half_time(...)` for bookkeeping parity with the other three
trackers (harmless no-ops for staleness purposes here, since `"half_time"`
isn't in Pattern 4's `stale_statuses`, but keeps `debug_state()` accurate).

**`MatchFinished` branch** (the pattern's actual evaluation point):

```python
targets4.on_finished(event.match_id)

first_half_total = None if event.first_half is None else event.first_half.home_goals + event.first_half.away_goals
second_half_total = None if event.second_half is None else event.second_half.home_goals + event.second_half.away_goals

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
            outcome=tracker4.last_outcome,  # "counting" | "reset" | "skipped"
        ),
    )
```

This enabled-check ordering mirrors Patterns 2/3 exactly: when
`PATTERN4_ENABLED=false`, the fire is logged (as a warning) but
`PatternArmed` is never published and `targets4.arm()` is never called at
all — so a disabled Pattern 4 does not silently accumulate `bet_targets`
entries it never bets on. `PATTERN4_ENABLED=false` therefore means "track
the streak and log every fire, never touch `TargetTracker` or place a bet"
— the same contract Pattern 2/3 already have.

### Config (`shared/config.py`) — new, independent knobs

| Variable | Default | Meaning |
|---|---|---|
| `PATTERN4_BET_LINE` | `16.5` | The Under line bet in `Total. Main game`. |
| `PATTERN4_BET_STAKE_AMOUNT` | `90` | FCFA staked per fired bet — independently configurable, matching Pattern 2/3's own independent stake knobs. |
| `PATTERN4_ENABLED` | `true` | Ships live from the first run, per explicit decision — consistent with how Pattern 1 and Pattern 3 both shipped directly live. |

### `shared/events.py` — two additive literal values, no new fields

- `PatternProgress.direction: Literal["at_or_under", "at_or_over", "equals"]`
  → add `"pair_count"`.
- `PatternProgress.outcome: Literal["qualifying", "reset", "skipped"]` → add
  `"counting"` (the "1 of 2 rounds collected, pair not yet complete" state
  — genuinely distinct from Pattern 1/2/3's "qualifying," which means
  "streak building toward firing," since Pattern 4 doesn't have a
  streak-length beyond the fixed pair of 2).
- No new event kinds, no changes to existing fields' meanings for Patterns
  1–3.

### `services/display/render.py` — one new branch, same shape as existing ones

`render_pattern_progress` gains a case for `direction == "pair_count"`:
narrate as e.g. `"Pattern 4: 2/4 values >= 9 this pair (need 3)"` using
`event.total` (the qualifying count) and `event.threshold` (9), instead of
the existing `<=`/`>=`-threshold wording that only makes sense for a single
scalar per round. `render_bet_placed`/`render_bet_failed`/
`render_bet_settled`/`render_pattern_armed` need no changes — they already
print `market_label`/`condition_label` verbatim.

## Testing

- `test_pattern.py`: new tests for `RoundPairStreakTracker` — the exact
  worked example from this spec (`[10,8]`+`[10,12]` → armed), the
  "one round entirely under 9" non-firing case, the "one round with only
  one value under 9" firing case, the skip-one-round-after-fire mechanic,
  and the `None`-half defensive reset. `PatternTracker`'s existing tests are
  untouched (new class, not a modification).
- `test_betting_api.py`: extend for `period=0` → `GameId=match_id` (no
  offset) in both the odds-lookup filter and the `MakeBetWeb` body; new
  tests for the `min_odds`/`poll_interval_seconds`/`is_stale` wait loop —
  odds below threshold then rising (bet placed at the price seen once it
  clears), odds never clearing before `is_stale()` flips true (returns
  `BetResult(success=False, ...)`, no request sent), and confirm
  `min_odds=None` (Patterns 1–3's implicit default) produces a
  byte-identical single-shot request to today with no `asyncio.sleep`
  ever invoked.
- `test_targeting.py`: no changes — `TargetTracker(stale_statuses=
  {"finished"})` is already covered by Pattern 2's existing tests for that
  exact parameterization.
- New: a `main.py`-level integration-style test (or extend whatever harness
  Pattern 2/3's wiring already has) confirming Pattern 4's `place()` call is
  genuinely non-blocking — e.g. a slow/never-resolving `min_odds` wait for
  one match doesn't delay a concurrent Pattern 1 bet's placement in the same
  test run.
- Update render/event round-trip smoke tests for the two new literal
  values.

## Known limitations (accepted, consistent with Patterns 1–3's stance)

- Pattern 4 state (`tracker4`, `targets4`, `pattern4_tasks`) is in-memory,
  single-process — a `bettor` restart loses its pair progress and abandons
  any in-flight odds-wait task, independently of Patterns 1–3's own
  same-shaped limitation.
- The odds-wait has no cap beyond the match itself finishing — an
  accepted, explicit decision, not an oversight; a round where the odds
  simply never reach 1.5 before finishing means no bet is placed for that
  fire, silently (logged, not alerted).
- Live from the first run, no dry-run gate — first real-money bet ever
  placed through the `period=0` code path and the first-ever use of the
  odds-wait loop; worth watching closely on its first live fire the same
  way Pattern 1's first live bets were watched.
- `MatchFinished.first_half`/`second_half` being optional in the type
  system but (believed) always populated in practice for a genuinely
  finished match is an existing open item inherited from Pattern 2's own
  spec, not newly introduced here — the implementation plan should confirm
  this empirically for `first_half` too, the same way Pattern 2's plan was
  asked to confirm it for `second_half`.
