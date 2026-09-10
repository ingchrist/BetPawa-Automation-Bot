# Pattern 2: "Total 2nd Half Under 7.5" streak-bettor — design

## Problem

Pattern 1 ("1st Half Over 6.5" streak) is implemented, live-trialed, and
proven the mechanism (streak detection → target resolution → real-money bet
placement via the site's own JSON API). This adds the mirror-image pattern
on **2nd-half** totals, running as a second, independent producer alongside
Pattern 1 on the same `services/bettor/` process — maximizing reuse of
Pattern 1's pieces rather than forking a parallel implementation.

This design also depends on a correctness fix made to Pattern 1's own
mechanism, written this session but not yet part of this spec's history:
every match actually has three ids — the raw `match_id` (whole-match,
ambiguous once live), `match_id + 1` (1st-half, unambiguous), and
`match_id + 2` (2nd-half, unambiguous). Pattern 2 only works at all because
`match_id + 2` exists and exposes its own period-scoped Total market —
without that fix, there would be no way to target the 2nd half specifically
once a match goes live. See `betting_api.py`'s module docstring for the full
investigation; this spec takes the corrected mechanism as a given.

## Pattern definition

Look at each finished round's **2nd-half combined goal total** (home + away
goals in the second half). Track a streak, mirroring Pattern 1 exactly but
in the opposite direction:

- 3 **consecutive** rounds each with 2nd-half total **≥ 8** → the pattern
  fires: bet on the **next** round's `Total. 2nd half` market, **Under 7.5**.
- Any round with 2nd-half total **≤ 7** breaks the streak back to 0.
- After a fire, the streak resets to 0 **and the very next finished round
  is excluded from counting** (it's the round that was just bet on) — the
  round after that starts a fresh streak from 0.

Same pure, order-dependent state machine shape as Pattern 1, just evaluated
on 2nd-half totals in the opposite direction.

## Architecture

No new service. Pattern 2 is a second, parallel set of handlers inside the
existing `services/bettor/main.py`'s `consume()` loop — own `PatternTracker`
instance, own `TargetTracker` instance, sharing the same `BetExecutor`
(stateless HTTP+auth, safe to share) and the same event bus subscription
Pattern 1 already uses.

```
                         xbet.match_events
                                │
                 ┌──────────────┼──────────────────────┐
                 ▼              ▼                       ▼
           aggregator      display                  bettor
         (unchanged)     (+ pattern2 panels)   consume() loop:
                                                 ├─ Pattern 1 handlers
                                                 │   (tracker1, targets1)
                                                 └─ Pattern 2 handlers
                                                     (tracker2, targets2)
                                                 both → shared BetExecutor
```

### `services/bettor/pattern.py` — `PatternTracker` generalized, not duplicated

The life-cycle (0→1→2→3→fire→skip-one-round→reset) is identical between
patterns; only the qualifying comparison direction differs.

- Rename the field `low_threshold` → `threshold` (internal to the class
  only — the existing `PATTERN_LOW_THRESHOLD` env var / `Config` field name
  is untouched; config→constructor-arg mapping in `main.py` is independent
  of the field's internal name).
- Add `direction: Literal["at_or_under", "at_or_over"] = "at_or_under"`.
  Pattern 1's behavior is unchanged by the default. The single qualifying
  check in `process()` becomes:
  - `at_or_under`: disqualifies (resets) when `total > threshold` — today's
    behavior, verbatim.
  - `at_or_over`: disqualifies (resets) when `total < threshold`.
  - In both directions, `total is None` also disqualifies (unchanged).
- Pattern 2 constructs it as
  `PatternTracker(threshold=8, streak_length=3, direction="at_or_over")`.
- `last_outcome`'s `"reset"`/`"qualifying"`/`"skipped"`/`"armed"` values and
  all other public surface (`streak`, `last_total`, `last_streak_totals`)
  are unchanged in meaning, just now direction-aware in how they're derived.

### `services/bettor/targeting.py` — `TargetTracker` reused unmodified, cross-pattern coordination lives in `main.py`

Pattern 2 gets its own `TargetTracker()` instance — same class, no code
changes — so its "next match to bet on" queue (`_latest_discovered`,
`_status`, `_pending`, `bet_targets`) is entirely independent of Pattern 1's.

**Mutual exclusion (new, this spec):** since both `TargetTracker` instances
observe the exact same `MatchDiscovered` stream, they will frequently
resolve to targeting the *same* match in the same round. Per the accepted
design decision, the two patterns must not both place a bet on the same
match — that would double stake exposure (90+90) on one match for no
reason the pattern logic actually intends. Rather than modify
`TargetTracker` itself (it stays a pure, pattern-agnostic building block),
`main.py` adds a coordination check at the point each pattern is about to
actually call the executor:

```
async def place(pattern_name, target, targets, other_targets, executor, ...):
    if target.match_id in other_targets.bet_targets:
        reason = f"mutual exclusion: match {target.match_id} already targeted by {other_pattern_name}"
        log.warning(reason)
        await bus.publish(..., BetFailed(match_id=target.match_id, reason=reason))
        return
    if targets.is_stale(target.match_id):
        ...  # existing stale-fire guard, unchanged
    # existing place_bet() call
```

Notes:
- This check runs *after* `target.match_id` has already been added to the
  firing pattern's own `bet_targets` (by `TargetTracker.arm()` or the
  pending-resolves-via-`on_discovered()` path) — so the firing pattern's own
  state still advances normally (streak already reset, skip-one already
  armed) even when the actual bet is suppressed. This matches the existing
  stale-fire guard's shape: the guard blocks the *bet*, not the pattern's
  internal bookkeeping.
- No new event type: reuses `BetFailed(match_id, reason)` exactly as the
  stale-fire guard already does, with a distinguishing reason string.
- Applies symmetrically: Pattern 1's `place()` checks against Pattern 2's
  `targets2.bet_targets`, and vice versa. Whichever pattern's fire resolves
  to a concrete target first (in code/event order) wins the match; the
  other is suppressed if it later resolves to the same one.
- Once a match is in a tracker's `bet_targets` (whether the bet was
  actually placed or suppressed by this guard), that tracker never retries
  it — same existing guarantee `TargetTracker` already provides.

### `services/bettor/betting_api.py` — `BetExecutor.place_bet()` extended, not forked

Add two parameters, both defaulted to preserve Pattern 1's exact current
behavior:

```
async def place_bet(
    self, match_id: int, home: str, away: str, stake: float,
    line: float = 6.5, period: Literal[1, 2] = 1, over: bool = True,
) -> BetResult:
    offset = FIRST_HALF_ID_OFFSET if period == 1 else SECOND_HALF_ID_OFFSET
    game_id = match_id + offset
    bet_type = TOTAL_OVER_T if over else TOTAL_UNDER_T
    ...
```

- New constant `SECOND_HALF_ID_OFFSET = 2`, alongside the existing
  `FIRST_HALF_ID_OFFSET = 1` in `betting_api.py`.
- `_current_odds()`'s market filter (`event.get("T") == ...`) becomes
  parameterized on `bet_type` the same way, so it looks up the Under row
  when `over=False`.
- The `MakeBetWeb` request body's `"Type"` field uses `bet_type` instead of
  the hardcoded `TOTAL_OVER_T`.
- Pattern 1's existing call sites (`main.py`'s Pattern 1 `place()`) need no
  changes — both new parameters default to Pattern 1's current values.
  Pattern 2 calls `place_bet(..., period=2, over=False)`.

### Config (`shared/config.py`) — new, independent knobs

Mirrors Pattern 1's naming exactly:

| Variable | Default | Meaning |
|---|---|---|
| `PATTERN2_HIGH_THRESHOLD` | `8` | A round qualifies when 2nd-half total ≥ this. |
| `PATTERN2_STREAK_LENGTH` | `3` | Consecutive qualifying rounds required to fire. |
| `PATTERN2_BET_LINE` | `7.5` | The Under line bet in `Total. 2nd half`. |
| `PATTERN2_BET_STAKE_AMOUNT` | `90` | FCFA staked per fired bet — independently configurable per explicit request, not hardcoded or shared with Pattern 1's stake. |

### Events (`shared/events.py`) — generalizing Pattern 1's hardcoded shapes

Pattern 1's `BetPlaced`/`BetFailed`/`BetSettled`/`PatternArmed`/
`PatternProgress` currently hardcode 1st-half-only assumptions
(`render_bet_placed` literally prints `"Total. 1st half Over {line}"`;
`BetSettled.first_half_total` is named for one pattern only). This is the
one place this design can't avoid touching already-shipped shapes:

- Add `market_label: str` to `BetPlaced`, `BetFailed`†, and `PatternArmed`
  — e.g. `"Total. 2nd half Under 7.5"` — built once in `main.py` per
  pattern (a small helper, not duplicated per call site) and printed
  verbatim by `render.py` instead of the current hardcoded text.
  († `BetFailed.match_id` is already optional for the "no target yet"
  case; `market_label` on `BetFailed` is best-effort — omitted/`None` when
  the failure happens before a market is even known, e.g. an auth-read
  failure.)
- Add `condition_label: str` to `PatternArmed` — e.g. `"3 consecutive
  rounds with 2nd-half total ≥ 8"` — replacing the render-side hardcoded
  "3 consecutive rounds at or under the threshold" text.
- Rename `BetSettled.first_half_total` → `period_total`, add
  `market_label: str` there too (so a settled panel can say which market
  settled, not just imply "1st half").
- Add `pattern_name: str` and `direction: Literal["at_or_under",
  "at_or_over"]` to `PatternProgress` so its render can pick the correct
  `≤`/`≥` wording and label which pattern's progress line is which (today
  `render_pattern_progress` hardcodes `≤`/reset-on-`>` wording that's only
  correct for Pattern 1's direction).
- No new event *kinds* — `MATCH_EVENT_TYPES` and the `MatchEvent` union are
  unchanged; only existing bettor-event fields change.

### `services/display/render.py` — follow the schema, don't special-case patterns

- `render_bet_placed`, `render_bet_failed`, `render_pattern_armed`,
  `render_bet_settled` print `event.market_label`/`event.condition_label`
  verbatim instead of the current hardcoded "1st half"/"≤ threshold" text.
- `render_pattern_progress` uses `event.pattern_name` and `event.direction`
  to choose `≤`/`>` (at_or_under) vs `≥`/`<` (at_or_over) wording, and to
  prefix the line with which pattern it's narrating (both patterns now emit
  `PatternProgress` on every round, so the line needs to say which one).
- `log_bet_event`'s plain-text mirror gets the same treatment for
  consistency between `data/bets.log` and the terminal.

### `services/bettor/main.py` — second parallel handler set + coordination

`consume()`'s single `if isinstance(event, ...)` chain grows a second set of
branches for Pattern 2, reusing the same event kinds `main.py` already
subscribes to but at a different point in each round's lifecycle than
Pattern 1:

- `MatchDiscovered` / `MatchStarted` / `MatchHalfTime` — Pattern 2's
  `TargetTracker` gets the same `on_discovered`/`on_started`/`on_half_time`
  calls Pattern 1's does, for identical reasons (target resolution,
  staleness tracking). No streak evaluation happens at `MatchHalfTime` for
  Pattern 2 — unlike Pattern 1, which deliberately moved off
  `MatchFinished` for latency reasons, Pattern 2 has no earlier event
  carrying a final 2nd-half score to move to.
- `MatchFinished` — this is Pattern 2's streak-evaluation point:
  `event.second_half.home_goals + event.second_half.away_goals` feeds
  `tracker2.process(...)`, and `BetSettled` for Pattern 2's outstanding
  target (if any) is published here too, mirroring how Pattern 1 evaluates
  and settles together at its own trigger event (`MatchHalfTime`). The
  implementation plan should confirm `MatchFinished.second_half` is
  reliably non-`None` in practice — `MatchFinished`'s model currently types
  it as `HalfScore | None` — before relying on it the same unconditional
  way `MatchHalfTime.first_half` (typed non-optional) is relied on today.

Both patterns' fire → target-resolve → `place()` call goes through the
shared, now-parameterized `place()` helper (pattern name / tracker /
other-tracker / period / over / config knobs, per the points above) rather
than a Pattern-1-only inline function.

## Testing

- `test_pattern.py`: extend for `direction="at_or_over"` — mirror the
  existing fire/break/skip/back-to-back cases with the comparison flipped;
  confirm `direction="at_or_under"` (the default) is byte-for-byte
  unchanged behavior via the existing test suite still passing unmodified.
- `test_betting_api.py`: extend for `period=2, over=False` →
  `GameId=match_id+2`, `Type=10` in both the odds-lookup filter and the
  `MakeBetWeb` request body; confirm `period=1, over=True` (defaults)
  still produces byte-identical requests to today.
- `test_targeting.py`: no changes needed — `TargetTracker` itself is
  unmodified.
- New: a `test_main.py`-level (or equivalent) test for the mutual-exclusion
  path — two trackers, same resolved match_id, confirm the second `place()`
  call short-circuits to `BetFailed` with the mutual-exclusion reason and
  never calls the executor.
- Update render/event round-trip smoke tests for the renamed
  (`first_half_total`→`period_total`) and added
  (`market_label`/`condition_label`/`pattern_name`/`direction`) fields.

## Known limitations (accepted, consistent with Pattern 1's stance)

- Pattern 2 state is in-memory, single-process — a `bettor` restart loses
  its streak progress independently of Pattern 1's (same accepted
  limitation Pattern 1 already has).
- Bet placement is fully live from the first run — no dry-run gate. This
  will be the first real-money bet ever placed through the `period=2,
  over=False` code path; worth watching closely on the first live fire the
  same way Pattern 1's first live bets were watched.
- The sub-id fix and this session's other in-flight changes (streak
  visibility, `MatchHalfTime`-based evaluation for Pattern 1,
  `TargetTracker.arm()` relaxation) remain uncommitted as of this spec,
  per explicit decision to commit everything together later rather than as
  a prerequisite to this work.
