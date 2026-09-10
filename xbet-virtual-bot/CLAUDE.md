# CLAUDE.md — Betting patterns on 1xbet virtual 3x3 FIFA: Pattern 1 live trial + Pattern 2 design ready to implement

This file exists so a fresh session can pick up this feature without
re-deriving context. It is scoped to this feature, not a general project
guide — see `README.md` for the bot's overall architecture
(collector/aggregator/bettor/display, event bus, config conventions).

**If you're picking this up cold: read this file fully before touching
anything.** This feature places real money automatically, a live trial was
mid-flight when a real correctness bug was found and fixed, and a second
pattern is designed but unimplemented. Get the state right before acting.

## TL;DR for a fresh session

1. A critical bug in `services/bettor/betting_api.py` was found and fixed
   this session (see "The sub-id bug" below) — **all real bets before this
   fix may have targeted the wrong market once a target match was already
   live**, not just pregame. The fix is written and tested but **not yet
   committed**.
2. Pattern 2 ("2nd Half Under" streak) is fully designed (see below) but
   **not implemented at all** — no code exists for it yet.
3. The bot was stopped by the controlling session after the bug was found,
   then restarted by the user (with the sub-id fix in place, though still
   uncommitted) to resume the live trial. As of this writing it's running
   (`collector`/`aggregator`/`bettor` all up). Check `./run.sh status`
   before assuming anything about live state — this will go stale the
   moment anyone stops/restarts it.
4. Nothing from this session has been committed. Review the diff before
   committing — don't assume it's all one logical commit (see "Uncommitted
   changes" below for the breakdown).

## What's being built

Two independent streak-detection betting patterns, both implemented as
producers on the same `services/bettor/` service (subscribes to
`xbet.match_events` over Redis, same as `aggregator`/`display`):

- **Pattern 1 ("1st Half Over" streak)** — 3 consecutive rounds with
  1st-half combined goal total ≤ a threshold (default 6) fires a bet on
  the *next* round's `Total. 1st half` market, `Over 6.5`. **Implemented,
  tested against real money, currently mid live-trial.**
- **Pattern 2 ("2nd Half Under" streak)** — the mirror image: 3
  consecutive rounds with 2nd-half combined goal total ≥ a threshold
  (default 8) fires a bet on the *next* round's `Total. 2nd half` market,
  `Under 7.5`. **Designed, not implemented.**

- **Design spec (Pattern 1):** `docs/superpowers/specs/2026-09-09-first-half-over-pattern-bettor-design.md`
- **Implementation plan (Pattern 1):** `docs/superpowers/plans/2026-09-09-first-half-over-pattern-bettor.md`
- **SDD execution ledger (Pattern 1, Tasks 1-8):** `../.superpowers/sdd/2026-09-09-first-half-over-pattern-bettor/progress.md`
  (one level ABOVE this directory — repo root is `BetPawa-Automation-Bot`,
  `xbet-virtual-bot` is a subdirectory) — authoritative task-by-task log
  for Tasks 1-8 (Task 9, this session's work, isn't reflected there — this
  file is the source of truth for everything after Task 8).
- **Pattern 2** has no spec/plan doc yet — the design below (from this
  session's brainstorming, presented and implicitly approved before the
  user asked to hand off to a fresh session) is the only record of it.
  A fresh session should very likely run it through
  `superpowers:brainstorming`'s written-spec step properly before
  implementing, rather than treating this section as a substitute.

## The sub-id bug (critical — read this before touching betting_api.py)

**Found and fixed this session, 2026-09-10, mid live-trial.** Two real,
settled bets (the original capture bet and a Red Bull–Chelsea verification
bet) had proven the betting mechanism basically worked, and Pattern 1 ran
for a while on that assumption. Then, while designing Pattern 2, a deeper
investigation into "which market does `G=17` actually mean" turned up a
mechanism nobody had previously identified:

**Every match on this site actually has three separate ids**, not one:

- `X` — the "main"/whole-match id. This is the *only* one the bulk feed
  (`Get1x2_VZip`) and our collector ever see — `match_id` throughout this
  codebase always means this one.
- `X + 1` — the 1st-half sub-game id. Independently queryable via
  `GetGameZip?id=X+1`, exposes its own period-scoped `T=9/10, G=17` Total
  Over/Under market. Real bets against this id settle against the 1st
  half specifically (confirmed via an actual bet slip: "Red Bull - Chelsea
  **1st half**", event "Total Over (6.5)", result "3:5", WON).
- `X + 2` — the 2nd-half sub-game id, same shape, scoped to the 2nd half.

**The bug:** `betting_api.py` was betting `GameId=match_id` (i.e. `X`)
directly. `X`'s own `G=17` market is the whole-match ("Main game") Total,
*not* either half specifically. Two things happened to mask this:

1. Both historically-verified real bets were placed while their target
   was still **pregame** (`TargetTracker.arm()`'s original, strict
   `"upcoming"`-only rule, before this session's change below). Pregame,
   before anything has happened, `X`'s Main-game Total line can coincide
   with `X+1`'s 1st-half line closely enough to fool an odds-based
   verification — this is believed to be why the original `G=17`
   verification (an exact 1.83/2.02 coefficient match against a live
   screenshot) looked airtight but wasn't proof of the right mechanism.
2. Earlier this same session, `TargetTracker.arm()` was deliberately
   relaxed (at the user's request) to also accept an already-`"started"`
   (live) target, not just `"upcoming"` — see the "TargetTracker
   relaxation" section below. That's what turned a latent, never-actually
   pregame-only issue into something anyone would eventually have hit.

**Proof, gathered today, in order of decreasing but still very high
confidence:**
- Two clean pregame A/B/C queries (`GetGameZip` on `X`, `X+1`, `X+2` for
  the same not-yet-started match, e.g. Celta vs Roma) showed `X`'s `G=17`
  lines (11.5-17.5) clearly distinct in magnitude from both `X+1` and
  `X+2` (5.5-9.5 each, close to each other but not identical).
- Two consecutive **real bets**, placed manually today by the user on the
  same live match (Anderlecht vs West Ham, main id `751673513`), captured
  via raw CDP: the "Total. 1st half" bet's actual request used
  `GameId=751673514` (`X+1`); the immediately-following "Total. 2nd half"
  setup used `GameId=751673515` (`X+2`). Neither used `751673513`. The
  1st-half bet's own error message (before the retry succeeded) named the
  event as `"Anderlecht - West Ham United 1st half"` — the site's own
  backend confirms the id-to-period mapping directly.
- Simultaneous side-by-side query of `X` and `X+2` on that same live match
  showed `X`'s `G=17` lines were *exactly* the `X+2` lines shifted by the
  known, already-final 1st-half score (a constant offset, identical
  coefficients) — i.e. `X`'s live Total is the whole-match cumulative,
  mathematically derived from the 2nd-half-only market plus the locked-in
  1st half.

**The fix (applied, tested, not committed):** `place_bet()` now computes
`game_id = match_id + FIRST_HALF_ID_OFFSET` (`FIRST_HALF_ID_OFFSET = 1`)
and uses that — never the raw `match_id` — for both the odds lookup and
the actual `MakeBetWeb` call. `FIRST_HALF_TOTALS_GROUP` was renamed to
`TOTALS_GROUP` since `G=17` isn't period-specific by itself; the period
comes entirely from which id you query. See the corrected module
docstring in `betting_api.py` for the full writeup — this section is a
summary. Tests updated/added in `tests/test_betting_api.py` (26/26
passing across the whole suite as of this session).

**What this means for Pattern 2 (and any future period-specific bet):**
always derive `GameId` from `match_id + offset` (1 for 1st half, 2 for
2nd half) — never trust `G=17` alone to disambiguate, and never assume a
match phase (pregame vs live) makes the raw `match_id` safe to bet
against.

**Not yet independently re-verified with a fresh real bet using the fixed
code** — the fix was validated by (a) unit tests and (b) a read-only
`GetGameZip(id=match_id+1)` smoke test against a real live match, but no
new real-money bet has been placed through the corrected `place_bet()` as
of this writing. Worth doing once the bot resumes, watched.

## Other changes made this session (Task 9), all uncommitted

Beyond the sub-id fix, three other changes went in during this session's
live trial, in this order:

1. **Real-time streak visibility in the terminal** (`shared/events.py`'s
   new `PatternProgress` event; `services/bettor/pattern.py`'s
   `PatternTracker` gained public `streak`/`last_total`/`last_outcome`
   read-after-`process()` attributes; `services/bettor/main.py` publishes
   `PatternProgress` on every non-firing round; `services/display/render.py`'s
   new `render_pattern_progress()` prints one bold-bright-red line per
   round, e.g. `streak: 1/3 (this round: 4 ≤ 6, qualifies)`). Deliberately
   doesn't print anything extra on the "armed" outcome — the existing
   `◈ PATTERN FIRED` panel already covers that moment. User-requested,
   implemented and confirmed working live.
2. **Pattern evaluation moved from `MatchFinished` to `MatchHalfTime`**
   in `services/bettor/main.py` — the pattern only ever needs 1st-half
   data, which is known (and, per `MatchHalfTime.first_half`, guaranteed
   non-optional) well before the 2nd half plays out. Waiting for
   `MatchFinished` delayed pattern firing by a full 2nd half's worth of
   wall-clock time, which was letting the real "next match to kick off"
   slip from `"upcoming"` to `"started"` before `TargetTracker.arm()` got
   a chance at it. `BetSettled` publishing moved along with it (same
   data dependency).
3. **`TargetTracker.arm()` relaxed** (`services/bettor/targeting.py`)
   from requiring `status == "upcoming"` to accepting anything
   `not in {"half_time", "finished"}` — i.e. `"upcoming"` *or*
   `"started"`. User-requested ("bet on the very next match regardless of
   whether it's in its live state or not"). This is what exposed the
   sub-id bug above to real risk; the fix above is what makes it actually
   safe to keep, so this was **not reverted**.

`tests/test_pattern.py` and `tests/test_targeting.py` were extended to
cover all three changes. Full suite: 26/26 passing.

## Pattern 2 design (approved in discussion, not yet implemented)

Maximize reuse of Pattern 1's pieces per the user's explicit instruction.
Not yet run through `writing-plans` — a fresh session should treat this as
a starting point, confirm it's still wanted, and probably write a proper
plan doc before implementing, especially since it touches already-shipped
event schemas.

1. **`PatternTracker` (`services/bettor/pattern.py`) — generalize, don't
   duplicate.** The life-cycle (0→1→2→3→fire→skip-one-round→reset) is
   identical between patterns; only the qualifying comparison direction
   differs. Rename `low_threshold` → `threshold` (internal to the class
   only — the existing `PATTERN_LOW_THRESHOLD` env var / `Config` field
   name stays untouched, config→constructor-arg mapping is independent of
   the field's internal name). Add `direction: Literal["at_or_under",
   "at_or_over"] = "at_or_under"` so Pattern 1's behavior is unchanged by
   default. Pattern 2 constructs it as
   `PatternTracker(threshold=8, streak_length=3, direction="at_or_over")`.
2. **`TargetTracker` — reused unmodified, just a second instance.**
   Pattern 2 gets its own `TargetTracker()` so its "next match to bet on"
   queue is independent of Pattern 1's.
3. **`BetExecutor.place_bet()` (`betting_api.py`) — extend, don't fork.**
   Add `period: Literal[1, 2] = 1` (selects `FIRST_HALF_ID_OFFSET` vs a
   new `SECOND_HALF_ID_OFFSET = 2`) and `over: bool = True` (selects
   `TOTAL_OVER_T` vs `TOTAL_UNDER_T`, both already defined in
   `services/collector/xbet_client.py`). Pattern 1's existing call sites
   need no changes (defaults preserve current behavior); Pattern 2 calls
   with `period=2, over=False`.
4. **Config (`shared/config.py`) — new, independent knobs**, mirroring
   Pattern 1's naming exactly: `PATTERN2_HIGH_THRESHOLD` (default 8),
   `PATTERN2_STREAK_LENGTH` (default 3), `PATTERN2_BET_LINE` (default
   7.5), `PATTERN2_BET_STAKE_AMOUNT` (default 90, independently
   configurable per the user's explicit request that stake be a variable,
   not hardcoded).
5. **Events (`shared/events.py`) — the one place this can't avoid
   touching Pattern 1's existing shapes.** `BetPlaced`/`BetFailed`/
   `BetSettled` currently hardcode 1st-half-only assumptions
   (`render_bet_placed` literally prints `"Total. 1st half Over {line}"`;
   `BetSettled.first_half_total` is named for one pattern only). Planned
   generalization:
   - Add `market_label: str` to `BetPlaced`, `BetFailed`, and
     `PatternArmed` (e.g. `"Total. 2nd half Under 7.5"`), built once in
     `main.py` and just printed verbatim by `render.py` instead of
     hardcoded text.
   - Add `condition_label: str` to `PatternArmed` (e.g. `"3 consecutive
     rounds with 2nd-half total ≥ 8"`).
   - Rename `BetSettled.first_half_total` → `period_total`, add
     `market_label` there too.
   - Add `pattern_name: str` and `direction: Literal["at_or_under",
     "at_or_over"]` to `PatternProgress` so its render can pick the right
     `≤`/`≥` wording and label which pattern's progress line is which
     (right now `render_pattern_progress` hardcodes `≤`/reset-on-`>`
     wording that's only correct for Pattern 1's direction).
6. **`services/bettor/main.py`** — the `consume()` loop gains a second,
   parallel set of handlers (own tracker, own targets, own
   `place()`-equivalent using `period=2, over=False`), sharing the *same*
   `BetExecutor` instance (it's stateless HTTP+auth, safe to share
   between patterns) and the *same* event bus/subscription.
7. **Tests** — extend `test_pattern.py` for `direction="at_or_over"`,
   extend `test_betting_api.py` for `period=2`/`over=False` →
   `GameId=match_id+2`, `Type=10`, update render/event round-trip smoke
   checks for the renamed/added event fields.

## Diagnostic scripts left in `scratch/` (throwaway, not shipped code)

All built this session to investigate the sub-id bug. Kept in case a
similar live-verification is ever needed again; none of them are part of
the shipped bot and none should be treated as reusable library code
without review:

- `raw_cdp_monitor.py` — pre-existing (Task 5). Passive raw-CDP network
  monitor, watches POST/PUT to `service-api` on an already-open tab.
- `click_capture_and_screenshot.py` — **the one new technique worth
  keeping in mind**: clicks a period tab via a plain DOM `.click()` over
  `Runtime.evaluate` (find the "Main game" leaf text node, walk up
  ancestors until a sibling leaf with the target tab's text is found,
  click it), then `Page.captureScreenshot`. This is a **different, and
  so far reliable, alternative to the Playwright-locator clicking that
  CLAUDE.md previously flagged as never made reliable** — worth trying
  first if a future task needs to drive a period tab again. Raw CDP,
  same-tab navigation/interaction, not a second observer session.
- `capture_2nd_half_markets.py`, `capture_passive_broad.py`,
  `click_2nd_half_and_capture.py`, `click_and_screenshot.py`,
  `track_g17_lifecycle.py` — earlier, superseded attempts at the same
  investigation (network-only capture without clicking, broader passive
  capture, first attempts at the click selector before it was fixed to
  walk up from "Main game"). Kept for reference on what didn't work and
  why; not needed for future work now that the sub-id mechanism is known.

## Constraints that still apply (unchanged from the plan's Global Constraints)

- Pattern 1: 3 consecutive rounds ≤ `PATTERN_LOW_THRESHOLD` (default 6)
  fires a bet on `Over PATTERN_BET_LINE` (default 6.5) for
  `BET_STAKE_AMOUNT` (default 90). After a fire, skip exactly one round
  before recounting. Pattern 2 mirrors this in the opposite direction —
  see design above.
- Bets go live from the first run — no dry-run gate (explicit user
  decision from the design phase, reaffirmed implicitly by continuing the
  live trial after the sub-id bug fix rather than asking for a dry-run).
- Stale-fire guard: if the target's relevant half is already over by the
  time the bet is attempted, `BetExecutor` finds no matching odds entry
  at the period-specific sub-id and returns
  `BetResult(success=False, reason="market not open (stale-fire guard)")`
  rather than betting into a closed market. This is now more robust than
  before, since it's checking the *correct* period-specific id, not the
  ambiguous main id.
- This is real money on the user's real account — treat every new kind of
  live action as needing explicit confirmation. This session confirmed
  separately before: starting the Task 9 trial, each round of live-CDP
  investigation that touched the operator's browser (navigating tabs,
  clicking), and the two diagnostic real bets placed today. The same bar
  applies going forward — don't assume standing consent covers a new kind
  of action just because a similar one was approved earlier.

## CDP operational notes (learned the hard way, still true — plus new ones from this session)

- `http://127.0.0.1:9222` is the CDP endpoint, already logged into
  1xbet.cm — never attempt a new login, never launch a new browser.
- **Orphaned tabs accumulate** from any script that opens a new tab and
  crashes before closing it. Check `curl -s http://127.0.0.1:9222/json/list`
  before live CDP work; close stale `.../esports/virtual...` page targets
  via `curl -s http://127.0.0.1:9222/json/close/<id>` if they've piled up.
  This session additionally reused an already-open idle tab (found by id,
  or by its `about:blank`/`chrome://newtab/` URL) and navigated it in
  place via raw CDP `Page.navigate`, rather than opening new tabs — a
  reasonable default to keep doing.
- **`pkill -f <pattern>` / `kill $(pgrep -f <pattern>)` is dangerous**
  inside a combined shell command that also contains `<pattern>` as
  literal text (matches the wrapping shell's own command line). Always
  kill by exact PID, or as a fully separate command with no shared
  substring with whatever starts next.
- League id `2860561`, URL path segment
  `2860561-fc-25-3x3-conference-league`. Live 3x3 matches found via
  `a[href*='2860561-fc-25-3x3-conference-league'][href*='/live/']` on
  `https://1xbet.cm/en/esports/virtual/fifa`.
- On a match page: period tabs are "Main game"/"1st half"/"2nd half" —
  the "1st half" tab disappears entirely once that half ends. Halves are
  short (~150s typically observed, but this session saw one run 350s+
  without finishing — don't assume a hard duration cap) and these
  matches are high-scoring, so time-sensitive work needs to move fast or
  poll continuously.
- **Clicking a period tab via raw-CDP `Runtime.evaluate` + a plain DOM
  `.click()` worked reliably this session** (see
  `scratch/click_capture_and_screenshot.py`) — this **supersedes** the
  earlier note that tab-clicking was never made reliable. That earlier
  finding was specifically about *Playwright's* locator-based click
  (`page.get_by_text(...).click()`), which still may not be reliable;
  driving the DOM directly via `Runtime.evaluate` sidesteps whatever was
  causing Playwright's wait/assertion logic to time out. Selector
  approach that worked: find the "Main game" tab as a leaf text node,
  walk up its ancestors until one contains a sibling leaf with the target
  tab's exact text, click that.
- **The site's odds/market API surface is bigger than previously
  documented.** `services/collector/xbet_client.py`'s docstring only
  documents `/service-api/LiveFeed/{Get1x2_VZip,GetGameZip}`. This
  session found the live match page's own frontend actually calls a
  *different* host prefix, `/cyber-api/mainfeedlive/web/cyber/v3/...`
  (`gameEvents`, `statistic`, `gameInfo`, `leftmenu/virtual`), with a
  different JSON envelope (`eventGroups`/`type`/`parameter`/`cf` instead
  of `E`/`T`/`P`/`C`) — but the *same* group numbers (e.g. `groupId: 17`
  for Totals). Not currently used by any shipped code (the collector and
  `betting_api.py` both still use `/service-api/...`, which works fine),
  but useful to know if `/service-api/...` ever stops returning what's
  needed for a specific market.
- `GetGameZip`'s `SC` block: `CPS` (current period string, e.g. `"1st
  half"`/`"2nd half"`), `PS` (list of `{Key: period_number, Value:
  {S1, S2, NF}}` per-period scores), `FS` (full/cumulative score `{S1,
  S2}`), `I` (status string, `"Pre-game betting"` when not yet started).
  Useful for any future live-state debugging.
