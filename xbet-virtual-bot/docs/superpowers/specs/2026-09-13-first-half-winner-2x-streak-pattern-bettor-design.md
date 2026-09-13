# Pattern 3: "1st Half Winner 2X" streak-bettor — design

## Problem

Patterns 1 and 2 both track a **numeric** streak (combined goal total
above/below a threshold) on a **Total Over/Under** market. Pattern 3 is
different on both axes: it tracks a **categorical** streak (which side the
Double Chance market for a half actually settled as — `"1X"`, `"2X"`, or
`"X"`), and it bets a **Double Chance** market, not Totals. The mean-reversion
premise: two consecutive rounds where the 1st half settles `"2X"` (away win
or draw) make the next round's 1st half more likely to settle `"1X"` (home
win or draw) — so the pattern fires a bet on `Double Chance. 1st half`,
selection `1X`.

This design maximizes reuse of Pattern 1/2's existing pieces
(`PatternTracker`'s fire/skip/reset life cycle, `TargetTracker`,
`BetExecutor`, the `place()` helper, mutual exclusion) rather than building a
parallel state machine, extending each piece only where its current shape
is genuinely numeric/Totals-specific and doesn't fit.

## Pattern definition

Look at each finished round's **1st-half Double Chance result** — derived
from `first_half.home_goals`/`away_goals` the same way
`services/display/render.py`'s `_double_chance_winner()` already computes it
for display: `"1X"` (home win or draw), `"2X"` (away win or draw), or `"X"`
(only possible when both would settle — i.e. an outright draw; not itself a
qualifying or bettable selection).

- **2 consecutive** rounds each with 1st-half winner **exactly `"2X"`** →
  the pattern fires: bet on the **next** round's `Double Chance. 1st half`
  market, selection **`1X`**, regardless of whether that next round is still
  upcoming or already live when the fire happens (same "arm now if known,
  else bet the next discovered match" semantics `TargetTracker.arm()`
  already provides for Pattern 1/2).
- Any round whose winner is **not** `"2X"` (i.e. `"1X"` or `"X"`) breaks the
  streak back to 0. No skip in this case.
- After a fire, the streak resets to 0 **and the very next finished round is
  excluded from counting entirely**, regardless of its own winner value —
  it's the round that was just bet on. The round after that restarts the
  count from 0.

This is byte-for-byte the same life cycle Pattern 1/2's `PatternTracker`
already implements (`_skip_next`, reset-on-non-qualifying, fire-at-
streak_length) — the only new thing is that "qualifies" here means
*category equality*, not a numeric comparison.

## Market mechanics (reverse-engineered read-only, no bet placed)

Confirmed live against `https://1xbet.cm/service-api/LiveFeed/GetGameZip`
for matches in the `2860561` (FC 25. 3x3. Conference League) league, using
the exact same authless read path `BetExecutor._current_odds()` already
uses:

- **Double Chance is `Group (G) = 8`**, with three selections keyed by
  `Type (T)`: `T=4` → `1X`, `T=5` → `12`, `T=6` → `2X`. `Param (P)` is
  always `null` for this group (unlike Totals, there's no line).
- It uses the **same per-half sub-game-id scheme** Totals already rely on
  (see `betting_api.py`'s module docstring): the raw `match_id` is the
  whole-match ("Main game") id, `match_id + FIRST_HALF_ID_OFFSET` (`+1`) is
  the unambiguous 1st-half sub-game, `match_id + SECOND_HALF_ID_OFFSET`
  (`+2`) the 2nd-half one. Pattern 3 always bets the 1st-half sub-game
  (`period=1`), same as Pattern 1.
- Verified against a live, undecided match (0-2 down at the time):
  1st-half sub-game odds were `T=4 (1X) C=4.37`, `T=5 (12) C=1.016`,
  `T=6 (2X) C=1.001` — consistent with "home team is losing, so 1X is
  possible-but-unlikely, and 2X (not-a-home-win) is near-certain." Also
  cross-checked against a 2nd-half sub-game on a different, more lopsided
  match, same `G=8`/`T=4,5,6` shape.

**Residual risk, explicitly accepted, not re-verified via a real placed
bet:** Pattern 1's original `MakeBetWeb` POST shape for Totals was
confirmed by watching one real, manually-placed bet over CDP (see
`betting_api.py`'s docstring). Pattern 3's Double Chance POST shape is
**inferred by symmetry** — the same `Type`/`Group`/`Param` triple that
correctly reads Double Chance odds is assumed to be what `MakeBetWeb`
expects to place one, exactly as already holds for Totals (Pattern 1/2's
own `bet_type`/`TOTALS_GROUP`/`line` triple round-trips between read and
write unchanged). This was explicitly discussed and accepted rather than
spending real money on a manual Double Chance confirmation bet first.

## Architecture

No new service. Pattern 3 is a third, parallel set of handlers inside the
existing `services/bettor/main.py`'s `consume()` loop — own
`PatternTracker` instance (categorical mode), own `TargetTracker` instance,
sharing the same `BetExecutor` and event-bus subscription Patterns 1/2
already use.

```
                         xbet.match_events
                                │
                 ┌──────────────┼──────────────────────┐
                 ▼              ▼                       ▼
           aggregator      display                  bettor
         (unchanged)   (+ pattern3 panels)     consume() loop:
                                                 ├─ Pattern 1 handlers
                                                 ├─ Pattern 2 handlers
                                                 └─ Pattern 3 handlers
                                                     (tracker3, targets3)
                                                 all three → shared BetExecutor
                                                 all three → 3-way mutual exclusion
```

### `services/bettor/pattern.py` — `PatternTracker` gains a categorical mode

The life cycle is unchanged; only the qualifying check gains a third branch:

```python
PatternDirection = Literal["at_or_under", "at_or_over", "equals"]

def _qualifies(self, total) -> bool:
    if self.direction == "equals":
        return total == self.threshold
    if self.direction == "at_or_under":
        return total <= self.threshold
    return total >= self.threshold
```

- `threshold: int | str`, `total`/`last_total`/`last_streak_totals` widen
  from `int` to `int | str` (`| None` where already optional). Pattern 1/2
  pass `int`s exactly as today — no behavior change, verified by the
  existing test suite passing unmodified.
- Pattern 3 constructs it as
  `PatternTracker(threshold="2X", streak_length=config.pattern3_streak_length, direction="equals")`.
- `total is None` still disqualifies (resets) regardless of direction,
  unchanged.

### New: `shared/markets.py` — `double_chance_winner()` moved, not duplicated

`services/display/render.py`'s `_double_chance_winner(home_goals,
away_goals) -> "1X" | "2X" | "X"` is the exact function Pattern 3 needs to
derive its streak input from `MatchHalfTime.first_half`. Rather than
duplicate this 4-line pure function into the bettor (risking the two
copies drifting), move it to `shared/markets.py` as `double_chance_winner()`
and have both `render.py` and `services/bettor/main.py` import it from
there. No behavior change to `render.py`'s output.

### `services/bettor/targeting.py` — `TargetTracker` reused unmodified

Pattern 3 gets its own `TargetTracker()` instance with the **default**
`stale_statuses={"half_time", "finished"}` — same as Pattern 1, since the
1st-half Double Chance market closes at half-time exactly like the 1st-half
Total market does. No changes to `targeting.py`'s classes.

**Mutual exclusion generalized from two-way to three-way.** Today
`mutual_exclusion_reason()` takes one `other_bet_targets` set. With three
independent patterns, any one of them can already have staked the same
match. `main.py`'s `place()` helper (see below) is generalized to check
against a *list* of `(other_targets, other_pattern_name)` pairs instead of
a single pair, calling the existing `mutual_exclusion_reason()` once per
other pattern — `targeting.py`'s pure function itself is unchanged, only
its call site fans out. This caps risk **per match** the same way the
two-way version did: the three patterns can each hold an open stake on
*different* matches simultaneously (up to 3x aggregate exposure vs. Pattern
1 alone), but never two patterns on the *same* match in the *same* round.

### `services/bettor/betting_api.py` — `BetExecutor.place_bet()` extended for a second market family

```python
DOUBLE_CHANCE_GROUP = 8
DOUBLE_CHANCE_1X_T = 4
DOUBLE_CHANCE_12_T = 5
DOUBLE_CHANCE_2X_T = 6

async def place_bet(
    self, match_id: int, home: str, away: str, stake: float,
    line: float | None = 6.5, period: Literal[1, 2] = 1, over: bool = True,
    bet_type: int | None = None, group: int = TOTALS_GROUP,
) -> BetResult:
    offset = FIRST_HALF_ID_OFFSET if period == 1 else SECOND_HALF_ID_OFFSET
    game_id = match_id + offset
    if bet_type is None:
        bet_type = TOTAL_OVER_T if over else TOTAL_UNDER_T
    ...
    coef = await self._current_odds(game_id, line, bet_type, group)
    ...
    # body["Param"] = line  (None for Double Chance, unchanged shape otherwise)
```

- `_current_odds()` gains the same `group` parameter, defaulting to
  `TOTALS_GROUP` so Pattern 1/2's call sites are unaffected; its filter
  becomes `event.get("T") == bet_type and event.get("P") == line and
  event.get("G") == group` — `P == line` already works correctly when both
  are `None`.
- Pattern 1/2's existing calls (`place_bet(..., line=6.5, period=1,
  over=True)` / `(..., line=7.5, period=2, over=False)`) are byte-identical
  in the request they produce — `bet_type`/`group` both default to their
  current derived values.
- Pattern 3 calls `place_bet(match_id=..., home=..., away=..., stake=...,
  line=None, period=1, bet_type=DOUBLE_CHANCE_1X_T, group=DOUBLE_CHANCE_GROUP)`.
  `over` is ignored when `bet_type` is explicitly given (documented in the
  docstring, not runtime-validated — consistent with this module's existing
  stance on `period` not being runtime-validated either, per the known
  limitations Pattern 2 already accepted).

### `services/bettor/main.py` — generalized `place()` + third handler set

Today's `place()` derives `market_label` and the `BetExecutor.place_bet()`
call internally from `period`/`over`/`line` — a shape that fits Totals but
not Double Chance. Generalize it to take a pre-built `market_label: str`
and a `place_call: Callable[[], Awaitable[BetResult]]` closure instead:

```python
async def place(
    target: MatchDiscovered, *, targets: TargetTracker,
    other_patterns: list[tuple[TargetTracker, str]],
    placed_matches: set[int], market_label: str, stake: float,
    place_call: Callable[[], Awaitable[BetResult]],
) -> None:
    for other_targets, other_pattern_name in other_patterns:
        conflict = mutual_exclusion_reason(target.match_id, other_pattern_name, other_targets.bet_targets)
        if conflict is not None:
            ...  # publish BetFailed, return — unchanged shape
    if targets.is_stale(target.match_id):
        ...  # unchanged
    result = await place_call()
    ...  # unchanged BetPlaced/BetFailed publishing
```

This is a **behavior-preserving refactor** of Pattern 1/2's existing call
sites (each now passes `market_label=_market_label(...)`,
`other_patterns=[(targets2, PATTERN2_NAME), (targets3, PATTERN3_NAME)]`,
and a `place_call` lambda wrapping the same `executor.place_bet(...)` call
they make today) — not new logic. Existing tests must confirm Pattern 1/2's
actual HTTP requests are unchanged after this refactor.

New Pattern 3 wiring, mirroring Pattern 1's shape exactly (both evaluate at
`MatchHalfTime`, both target the 1st-half sub-game):

- `MatchDiscovered` — `targets3.on_discovered(event)`; if it returns a
  target (a fire was pending one), call `place()`.
- `MatchStarted` — `targets3.on_started(event.match_id)`.
- `MatchHalfTime` — `targets3.on_half_time(event.match_id)`; derive
  `winner_1h = double_chance_winner(event.first_half.home_goals,
  event.first_half.away_goals)`; if `event.match_id in placed_matches3`,
  publish `BetSettled(won=(winner_1h == "1X"), period_total=first_half_total,
  market_label=_double_chance_market_label(1))` (goal total carried purely
  for display parity with Pattern 1/2's settled panels, not itself the
  win/loss determinant); feed `winner_1h` into `tracker3.process(...)`;
  on fire, publish `PatternArmed`/call `targets3.arm()`/`place()` exactly
  like Pattern 1's `MatchHalfTime` branch does; otherwise publish
  `PatternProgress`.
- `MatchFinished` — `targets3.on_finished(event.match_id)`, alongside the
  existing `targets.on_finished()`/`targets2.on_finished()` calls.

New label helpers alongside the existing `_market_label`/`_condition_label`:

```python
def _double_chance_market_label(period: int) -> str:
    half = "1st half" if period == 1 else "2nd half"
    return f"Double Chance. {half} 1X"

def _winner_condition_label(streak_length: int) -> str:
    return f"{streak_length} consecutive rounds with 1st half winner 2X"
```

`PATTERN3_NAME = "1st_half_winner_2x_streak"`, following the existing
`PATTERN1_NAME`/`PATTERN2_NAME` convention (and its already-accepted,
parked limitation: the name doesn't encode a bet line the way 1/2's do,
since Pattern 3 has none).

### Config (`shared/config.py`) — new, independent knobs

| Variable | Default | Meaning |
|---|---|---|
| `PATTERN3_STREAK_LENGTH` | `2` | Consecutive `"2X"` 1st-half rounds required to fire. |
| `PATTERN3_BET_STAKE_AMOUNT` | `90` | FCFA staked per fired bet — independently configurable, not shared with Pattern 1/2's stake. |
| `PATTERN3_ENABLED` | `true` | Kill switch, mirroring `PATTERN2_ENABLED`'s exact semantics: when `false`, Pattern 3 keeps tracking the streak and publishing `PatternProgress`/`PatternArmed`, but never arms a target or calls `BetExecutor`. Defaults to `true` per explicit decision — Pattern 3 goes live immediately once this ships, same stance as Pattern 1 and Pattern 2. |

The trigger winner (`"2X"`) and bet selection (`1X`) are not made
independently configurable — they're the pattern's own definition (mirror-
image mean reversion), not tunable knobs, consistent with how Pattern 1's
`over=True`/Pattern 2's `over=False` sidedness is hardcoded per-pattern
rather than exposed as an env var.

### Events (`shared/events.py`) — widening, not adding new kinds

- `PatternProgress.direction` widens to `Literal["at_or_under",
  "at_or_over", "equals"]`; `.threshold` widens to `int | str`; `.total`
  widens to `int | str | None`.
- `PatternArmed.qualifying_totals` widens to `list[int | str]` (Pattern 3
  publishes `["2X", "2X"]`).
- No changes to `BetPlaced`/`BetFailed`/`BetSettled`'s shapes — Pattern 3
  reuses `market_label`/`period_total`/`won` exactly as Pattern 1/2 do.
- No new event *kinds* — `MATCH_EVENT_TYPES`/`MatchEvent` unchanged.

### `services/display/render.py` — one small addition, no special-casing

`render_pattern_progress` picks `qualify_cmp`/`reset_cmp` off
`event.direction`; add the `"equals"` case: `qualify_cmp = "=="`,
`reset_cmp = "!="`. The existing `f"{this_round} {cmp} {event.threshold}"`
templating already works unchanged for string values (e.g. `"this round:
2X == 2X, qualifies"`). No other render function needs to change —
`market_label`/`condition_label` already carry pattern-specific text
verbatim, per Pattern 2's own generalization.

## Testing

- `test_pattern.py`: new tests for `direction="equals"` — fire on two
  consecutive `"2X"` rounds, reset on a `"1X"`/`"X"` round (no skip),
  fire→skip-one→resume mirroring the existing
  `test_fire_then_skip_one_round_then_restart` shape exactly but with
  string totals; confirm numeric directions are unaffected by the type
  widening via the existing suite passing unmodified.
- `test_betting_api.py`: new tests for `bet_type=DOUBLE_CHANCE_1X_T,
  group=DOUBLE_CHANCE_GROUP, line=None` — confirms `GameId=match_id+1`,
  `Type=4`, `Param=null` in the `MakeBetWeb` body, and that the odds-lookup
  filter matches a `G=8`/`P=None` `GetGameZip` entry; confirm Pattern 1/2's
  default-args call shape is still byte-identical after the `bet_type`/
  `group` parameters are added.
- `test_targeting.py`: no new class behavior, but add a test confirming
  `mutual_exclusion_reason()` composes correctly when checked against two
  separate `other_bet_targets` sets in sequence (i.e. the fan-out `place()`
  will do), rather than only ever one.
- New: `shared/markets.py`'s `double_chance_winner()` gets its own direct
  unit tests (home win → `"1X"`, away win → `"2X"`, draw → `"X"`) —
  currently only exercised indirectly through `render.py`.
- `main.py` wiring: extend whatever integration-level test coverage
  Pattern 2's wiring got (per the SDD ledger) to Pattern 3's handlers,
  including a three-pattern mutual-exclusion scenario (pattern A fires,
  patterns B and C's would-be targets on the same match both get
  `BetFailed` with a mutual-exclusion reason).
- Full suite must stay green throughout, including all existing Pattern
  1/2 tests — the `place()`/`BetExecutor.place_bet()` refactors here are
  meant to be behavior-preserving for Pattern 1/2, and the suite is what
  proves that.

## Known limitations (accepted, consistent with Patterns 1/2's stance)

- Pattern 3 state is in-memory, single-process — a `bettor` restart loses
  its streak progress independently of Pattern 1/2's (same accepted
  limitation they already have).
- Bet placement is fully live from the first run — no dry-run gate, and
  `PATTERN3_ENABLED` defaults to `true` (explicit decision, see the config
  table above). This will be the first real-money bet ever placed on a
  Double Chance market through this codebase; worth watching closely on
  the first live fire, same as Pattern 1/2's first live bets were.
- The Double Chance `MakeBetWeb` request shape is inferred by symmetry with
  Totals, not confirmed via a real captured bet (see "Market mechanics"
  above) — explicitly accepted risk, not a gap to close before shipping.
- `PATTERN3_NAME` doesn't encode a bet line (there isn't one) — differs
  slightly from `PATTERN1_NAME`/`PATTERN2_NAME`'s existing (already parked)
  line-baking quirk, but for a different reason; not itself a bug.
- Aggregate exposure across all three patterns can now reach 3x a single
  pattern's stake if each independently targets a different match in the
  same round — same accepted shape as the 2x figure Pattern 2's own spec
  already documented, extended by one more pattern.
