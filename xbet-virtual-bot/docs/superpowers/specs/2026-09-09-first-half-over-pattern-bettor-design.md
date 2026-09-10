# Pattern 1: "Total 1st Half Over 6.5" streak-bettor — design

## Problem

The bot currently only observes and displays FC 25 3x3 Conference League
rounds (`services/collector` → `services/aggregator` → `services/display`).
This adds the first automated betting pattern on top of that stream,
following the roadmap note in `README.md`: "a betting service subscribing
to `xbet.match_events` ... is the natural next addition."

## Pattern definition (as specified by the user)

Look at each finished round's **1st-half combined goal total** (home +
away goals in the first half — the "total for 1st half" column already
shown in the terminal's RESULT grid). Track a streak:

- 3 **consecutive** rounds each with 1st-half total **≤ 6** → the pattern
  fires: bet on the **next** round's `Total. 1st half` market, **Over 6.5**.
- Any round with 1st-half total **≥ 7** breaks the streak back to 0.
- After a fire, the streak resets to 0 **and the very next finished round
  is excluded from counting** (it's the round that was just bet on) — the
  round after that starts a fresh streak from 0.

This is a pure, order-dependent state machine over the sequence of
finished rounds; no team-name or match-id specific state.

## Architecture

New service `services/bettor/`, wired the same way as `aggregator` and
`display` — an independent process subscribing to `xbet.match_events`
over Redis, publishing its own new event kinds back onto the same channel
so `display` (or anything else) can render them without new plumbing.

```
                         xbet.match_events
                                │
                 ┌──────────────┼──────────────┐
                 ▼              ▼               ▼
           aggregator      display          bettor (new)
         (unchanged)     (+ new panels)    - PatternTracker (pure)
                                            - BetExecutor (Playwright/CDP)
                                            - publishes: PatternArmed,
                                              BetPlaced, BetFailed, BetSettled
```

### `services/bettor/pattern.py` — `PatternTracker`

Pure Python, no I/O — same spirit as `MatchStateMachine` in
`aggregator/state.py`. Fed one `first_half_total: int | None` per finished
round, in finish order.

State: `streak: int` (0-3), `skip_next: bool`.

```
process(first_half_total) -> fired: bool
  if skip_next:
      skip_next = False
      return False
  if first_half_total is None:
      streak = 0
      return False
  if first_half_total <= PATTERN_LOW_THRESHOLD:      # default 6
      streak += 1
      if streak == PATTERN_STREAK_LENGTH:            # default 3
          streak = 0
          skip_next = True
          return True   # FIRE
      return False
  streak = 0
  return False
```

### `services/bettor/targeting.py` — next-match resolution

Tracks the latest `MatchDiscovered` payload (the single "announced next"
match, same one `display` shows as UPCOMING) and the last-seen status
(`upcoming`/`started`/`half_time`/`finished`) per `match_id` it has
observed, purely from the events it already receives.

- On `PatternArmed` (pattern fired): if a distinct, not-yet-used
  `latest_discovered` match exists, hand it to the executor immediately.
  Otherwise mark `pending = True`.
- On `MatchDiscovered`: update `latest_discovered`; if `pending`, hand
  this match to the executor and clear `pending`.
- **Stale-fire guard** (per user's decision): immediately before actually
  invoking the browser executor, re-check that target match's last-seen
  status. If it's already past `started` with a `half_time` or `finished`
  event seen for it, abort — log a `BetFailed` (reason:
  `"stale: target match already past 1st half"`) instead of clicking into
  a closed/irrelevant market.
- Guards against double-betting the same `match_id` twice.

### `services/bettor/betting_api.py` — `BetExecutor`

**Superseded design.** The original plan below (UI-click automation)
proved too flaky live to trust with real money — Vue-SPA re-render races
on the "1st half" tab click, plus `connect_over_cdp` hanging once orphaned
tabs from exploration accumulated. The user decided live to switch to the
same philosophy `services/collector/xbet_client.py` already uses for
reads: call the site's own JSON API directly, reverse-engineered by
capturing one real, authorized bet (90 FCFA, "Total. 1st half", Over 6.5)
via a network monitor while the user placed it manually. Full derivation,
the captured request/response shapes, and the auth-token sourcing are
documented in `betting_api.py`'s module docstring — summary:

- `POST /service-api/LiveBet/Secure/MakeBetWeb` places the bet. Body
  carries `Events: [{GameId, Type: 9, Coef, Param: <line>, ...}]` (`Type:
  9` matches `xbet_client.py`'s documented `TOTAL_OVER_T`), `Summ: <stake>`,
  and `CheckCf: 2` (coefficient-staleness tolerance — matches the verified
  working request; needed because this client always fetches odds then
  places a beat behind the live line). Response carries `Success: bool`,
  `Error`, `ErrorCode`, and on success a `Coupon` with the placed bet's id
  and the account's new balance.
- Auth rides on two headers, both readable straight from the browser with
  no fingerprint-regeneration logic needed: `x-auth: Bearer <JWT>` is
  byte-identical to the `access_token` cookie; `x-hd: <token>` is
  byte-identical to the `.token` field inside `localStorage["fp_d"]`. Both
  expire together (~4h window, auto-refreshed by the page's own JS while
  it stays open) — so `BetExecutor` re-reads them fresh via a read-only CDP
  touch (`context.cookies()` + one `page.evaluate` reading `localStorage`,
  no navigation, no new tab) immediately before every bet rather than
  caching past that window.
- The current coefficient for `(match_id, line)` is fetched fresh from
  `GetGameZip` (already used for reads) immediately before placing, filtered
  to `T=9, P=<line>, G=17`. `G=17` is confirmed (2026-09-10) to mean "Total.
  1st half": a pre-match UI snapshot showing Over 6.5 = 1.83 / Under 6.5 =
  2.02 matched, coefficient-for-coefficient, `{T:9,P:6.5,C:1.83,G:17}` /
  `{T:10,P:6.5,C:2.02,G:17}` in the same match's `GetGameZip`. Earlier
  mid-match samples had shown `G=17` at much higher lines (12.5-20.5+) and
  were briefly mistaken for a different, full-match market — these 3x3
  virtual matches are simply high-scoring enough that a single half's own
  total climbs past that range within the first couple of minutes; the
  drift is real game state, not a wrong filter. No matching entry (market
  closed) is treated as the stale-fire guard and reported as
  `BetResult(success=False, reason="market not open (stale-fire guard)")`.
- A second, independently-connected Playwright session watching an
  already-open tab was tried first as the network-capture mechanism and
  found to silently miss cross-session Network events (reproduced twice);
  the actual capture used raw CDP over a direct websocket instead. This
  doesn't affect `BetExecutor` itself (it only ever holds one Playwright
  connection at a time, for the auth touch), but is recorded here since
  it's a real gap in a pattern ("watch this tab from a second process")
  that might otherwise seem reusable elsewhere in this codebase.

`place_bet(match_id, home, away, stake, line=6.5) -> BetResult` — same
signature as originally planned, so `main.py` (Task 6) doesn't depend on
which mechanism is behind it. Every failure path (auth read, odds lookup,
the placement request itself, or a `Success: false` response) returns
`BetResult(success=False, reason=...)` rather than raising, same contract
as originally planned.

Unit-tested (unlike the original UI-driven design, which the spec called
"not meaningfully unit-testable") — `tests/test_betting_api.py` mocks the
HTTP layer with `httpx.MockTransport` and injects a fake auth reader, so
the full success/failure matrix runs offline with no real browser or
network. Verified once against the real API via the capture-bet described
above; a second real bet to verify this replacement client end-to-end
needs separate explicit user authorization before it happens (the
capture-bet's authorization does not extend to it).

<details>
<summary>Original UI-click design (superseded, kept for history)</summary>

Connects once at startup via `playwright.chromium.connect_over_cdp(CDP_URL)`
to the already-running, already-logged-in Chrome (no new login flow —
verified live against the real site: this box's browser is *not*
geo-blocked, unlike the plain-HTTP route documented in the main README's
"How data is sourced"). Opens **one dedicated tab** it owns for the
service's lifetime, separate from the user's own tabs.

`place_bet(match_id, home, away, stake, line=6.5) -> BetResult`:

1. Navigate to the match (verified live: matches list at
   `/en/esports/virtual/fifa` exposes `a[href*='/live/{match_id}-...']`
   for currently-live matches — used as the primary lookup; a direct URL
   build from `league_id`/`match_id` is the fallback. Not-yet-live
   ("upcoming") matches need the equivalent pre-match list lookup —
   confirmed the concept works, exact selector to be hardened against the
   live site during implementation).
2. Click the **1st half** period tab, scoped to the match's own info
   panel (not the sidebar match list — this ambiguity was hit and noted
   during design exploration; implementation must scope the locator to
   avoid it).
3. Open **Total. 1st half**, click the **Over 6.5** row (line
   configurable) — confirmed live: this exact label/section exists as
   described by the user.
4. Enter `stake` into the bet-slip stake input, click place/confirm.
5. Detect success vs. an error banner (odds changed, insufficient funds,
   market suspended) from the slip's own feedback.

Every step is wrapped so a selector miss, timeout, or site-side rejection
returns `BetResult(success=False, reason=...)` — it never raises into the
service's event loop.

</details>

### New events (`shared/events.py`)

| kind | Fired when | Key fields |
|---|---|---|
| `pattern_armed` | `PatternTracker.process()` returns fired | `qualifying_totals: list[int]` (the 3 streak values), `pattern_name` |
| `bet_placed` | `BetExecutor.place_bet()` succeeds | `match_id`, `home`, `away`, `stake`, `line`, `odds` |
| `bet_failed` | placement skipped or failed | `match_id?`, `reason` |
| `bet_settled` | the target match's own `MatchFinished` arrives | `match_id`, `won: bool`, `first_half_total` |

All four travel on the existing `xbet.match_events` channel — one more
discriminated union member, same pattern as the five that already exist.

### Config (`shared/config.py` + `.env.example`)

| Variable | Default | Meaning |
|---|---|---|
| `BET_STAKE_AMOUNT` | `90` | FCFA staked per fired bet. |
| `PATTERN_STREAK_LENGTH` | `3` | Consecutive qualifying rounds required to fire. |
| `PATTERN_LOW_THRESHOLD` | `6` | A round qualifies when 1st-half total ≤ this. |
| `PATTERN_BET_LINE` | `6.5` | The Over line clicked in `Total. 1st half`. |
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the logged-in browser. |

### Logging / audit trail

`data/bets.log` — plain text, one block per pattern fire, same
"tracked in git" treatment as `data/result.log`: timestamp, the 3
qualifying rounds' totals, target match, stake, odds at click-time,
PLACED/FAILED/SKIPPED, later appended with WON/LOST once `bet_settled`
arrives. Written by the `display` service (consistent with it owning
`result.log` today) from the new events, not by `bettor` directly.

### Display integration (`services/display/render.py` + `main.py`)

Subscribes to the four new event kinds, renders a rich panel per event:
pattern-fired summary, bet-placed/failed confirmation, and a settled
won/lost panel — same visual language as the existing RESULT grid.

## Testing

`PatternTracker` is pure and gets real unit tests (first in the repo,
following the README's own note that this was overdue for
`MatchStateMachine`): fire on 3-in-a-row, break-and-restart mid-streak,
fire→skip→restart, back-to-back fires, `None` total handling.

`BetExecutor` isn't meaningfully unit-testable (drives a real page against
a real logged-in session); validated by one watched live trial run during
implementation, then left running.

## Known limitations (accepted for v1, consistent with existing project stance)

- Pattern/streak state is in-memory, single-process — a `bettor` restart
  loses streak progress, same accepted limitation the aggregator already
  has for its own state.
- Bet placement is fully live from the first run (per explicit user
  decision) — no dry-run gate.
- Exact browser selectors for the "1st half" tab and the not-yet-live
  ("upcoming") match lookup need hardening against the real site during
  implementation; the market path itself (`Total. 1st half` → `Over 6.5`)
  was confirmed live during design.
