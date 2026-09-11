# CLAUDE.md — Betting patterns on 1xbet virtual 3x3 FIFA: Pattern 1 + Pattern 2 both live

This file exists so a fresh session can pick up this feature without
re-deriving context. It is scoped to this feature, not a general project
guide — see `README.md` for the bot's overall architecture
(collector/aggregator/bettor/display, event bus, config conventions).

**If you're picking this up cold: read this file fully before touching
anything.** This feature places real money automatically. Both patterns are
implemented, tested, reviewed, committed, and — as of this writing — **both
are live and have already fired real bets**. Get the state right before
acting; check `./run.sh status` and `tail logs/bettor.log` before assuming
anything about current live state, since both go stale the moment anyone
stops/restarts the bot or a new round finishes.

## TL;DR for a fresh session

1. **Pattern 2 ("2nd Half Under 7.5" streak) is fully implemented**, not just
   designed. It was built across an 8-task plan executed via
   `superpowers:subagent-driven-development` in the prior session — every
   task passed its own task-scoped review, plus a final whole-branch review
   with one fix wave. All of it is committed and pushed to
   `origin/xbet-virtual-bot` (currently at `2075e7f`). Full suite: 40/40
   passing.
2. **Both patterns are live right now** and have both already fired real
   money:
   - Pattern 1 fired 2026-09-10 22:59, bet 90 on Fenerbahce vs Roma @ 1.52
     ("Total. 1st half Over 6.5") — **WON**.
   - Pattern 2 fired for the very first time ever 2026-09-11 01:26 (streak
     `[9, 8, 11]`), bet 90 on Heart of Midlothian vs West Ham United @ 1.815
     ("Total. 2nd half Under 7.5") — **placed successfully, pending as of
     this writing** (match not yet finished). Check `data/bets.log` for
     whether it's settled by the time you read this.
   - The bot was restarted onto the new two-pattern code by the user
     directly (bettor.log shows `starting Pattern 1` / `starting Pattern 2`
     at 21:55:19), **not through the plan's own Task 8 live-trial
     checkpoint** — the controlling session at the time had that checkpoint
     open, gating on explicit authorization, and the user restarted
     independently before that walkthrough happened. No failures or
     mutual-exclusion conflicts have shown up in the log so far, and the
     generalized event rendering (`market_label` etc.) is displaying
     correctly live.
3. **Source of truth for how Pattern 2 was built**, in order of how much
   detail each one carries:
   - `docs/superpowers/specs/2026-09-10-second-half-under-pattern-bettor-design.md`
     — the design (including an amendment made mid-plan-writing: `TargetTracker`
     gained a `stale_statuses` parameter, since Pattern 2's 2nd-half market
     opens at half-time rather than closing then).
   - `docs/superpowers/plans/2026-09-10-second-half-under-pattern-bettor.md`
     — the 8-task implementation plan, with full code for every change.
   - `.superpowers/sdd/2026-09-10-second-half-under-pattern-bettor/progress.md`
     — the execution ledger: every task's dispatch/review outcome, every
     ruling made and why, the full parked/deferred findings list from the
     final review. **Read this before assuming anything about known issues
     or design tradeoffs** — it's more current than this file for that.
4. A process defect was found and fixed *during* the SDD execution, not
   before it: this repo had months of pre-existing uncommitted real-money
   code (the sub-id fix + prior session's live-trial changes) sitting
   directly in the files Pattern 2 needed to touch. The first task's commit
   swept all of that in invisibly, which made its review flag a phantom
   "massive unauthorized rewrite." Fixed by splitting history into a clean
   baseline commit (`22ec6e8`) landing that pre-existing work on its own,
   followed by Pattern 2's own commits on top — verified byte-identical
   before/after the split, so nothing was lost. This is *why* Pattern 2's
   git history starts with a commit titled "Land the sub-id fix and Task
   9's live-trial changes" that isn't really about Pattern 2 itself.
5. **Two real correctness bugs were caught and fixed along the way** (not
   false alarms — both are in the ledger's "Rulings I made" with full
   detail):
   - **Phantom `BetSettled`** (caught by Task 6's own review): a bet
     blocked by mutual exclusion (or the pre-existing stale-fire race)
     still had its `match_id` recorded as "targeted," so its match
     finishing later would publish a fabricated win/loss into
     `data/bets.log` — for *both* patterns, since the bug pre-existed in
     Pattern 1's own already-approved code, not just new Pattern 2 code.
     Fixed via `placed_matches`/`placed_matches2` sets that only populate
     at an actual successful `BetPlaced`, gating settlement on that instead
     of on the "was this match ever targeted" set.
   - **`shared/bus.py` had zero tolerance for its own breaking schema
     change** (caught by the final whole-branch review): Pattern 2's event
     schema generalization made several bettor-event fields required that
     weren't before. `EventBus.subscribe_match_events()`/
     `subscribe_snapshots()` had no exception handling around
     `model_validate_json()` — a `ValidationError` from an old-schema
     publisher (e.g. a `display` process left running across a restart)
     would silently kill the whole subscription loop. Fixed: both now
     catch `ValidationError` and `continue` past an incompatible message
     instead of dying.

## Both patterns' mechanics, in brief (see the spec for the full rationale)

- **Pattern 1** — "1st Half Over 6.5" streak. 3 consecutive rounds with
  1st-half total ≤ `PATTERN_LOW_THRESHOLD` (default 6) fire a bet on the
  next round's `Total. 1st half`, `Over PATTERN_BET_LINE` (default 6.5).
  Evaluates at `MatchHalfTime`.
- **Pattern 2** — "2nd Half Under 7.5" streak, the mirror image. 3
  consecutive rounds with 2nd-half total ≥ `PATTERN2_HIGH_THRESHOLD`
  (default 8) fire a bet on the next round's `Total. 2nd half`, `Under
  PATTERN2_BET_LINE` (default 7.5). Evaluates at `MatchFinished`, not
  `MatchHalfTime` like Pattern 1 — there's no earlier event carrying a
  final 2nd-half score.
- Both run as independent `PatternTracker`/`TargetTracker` pairs in the same
  `services/bettor/main.py` process, sharing one `BetExecutor` (stateless
  HTTP+auth). `PatternTracker` gained a `direction` param
  (`"at_or_under"`/`"at_or_over"`), `BetExecutor.place_bet()` gained
  `period`/`over` params, `TargetTracker` gained `stale_statuses` — all
  extensions with backward-compatible defaults, not forks.
- **Mutual exclusion**: the two patterns never both bet on the same match
  in the same round. `mutual_exclusion_reason()` (a pure function in
  `services/bettor/targeting.py`, not a `TargetTracker` method) checks the
  *other* pattern's `bet_targets` before either pattern's `place()` ever
  calls the executor. Whichever pattern's target resolves first wins the
  match; the other logs a `BetFailed` with a `mutual exclusion: ...`
  reason. This caps risk **per match**, not in aggregate — the two
  patterns can each hold an open stake on *different* matches
  simultaneously, roughly doubling aggregate exposure vs. Pattern 1 alone
  (documented in README.md's Pattern 2 section).
- Independent stakes/thresholds: `PATTERN2_HIGH_THRESHOLD`,
  `PATTERN2_STREAK_LENGTH`, `PATTERN2_BET_LINE`, `PATTERN2_BET_STAKE_AMOUNT`
  — see `.env.example`.

## Known, deliberately-not-fixed issues (parked in the SDD ledger, none load-bearing)

Full list with reasoning is in the ledger; the two most likely to matter to
a future session:
- `PATTERN1_NAME`/`PATTERN2_NAME` constants (in `services/bettor/main.py`)
  bake the configured bet line into the name (e.g.
  `"1st_half_over_6.5_streak"`) — if `PATTERN_BET_LINE`/`PATTERN2_BET_LINE`
  are ever overridden via `.env`, the name silently desyncs from the actual
  line. Pre-existing for Pattern 1; newly visible since `PatternProgress`
  now prints `pattern_name` every round.
- `BetExecutor.place_bet()`'s `period` branching (`if period == 1 else`)
  isn't runtime-validated against `{1, 2}` — any other value silently maps
  to the 2nd half. Plan-mandated shape, not hardened.

## Diagnostic scripts in `scratch/`

Unchanged from before — see the git history around 2026-09-09/10 if a
similar live-verification is ever needed again (raw CDP network capture,
DOM-click-via-`Runtime.evaluate` technique). Nothing there is shipped code;
`scratch/` is gitignored.

## A knowledge graph exists for this codebase (graphify)

A third-party tool called `graphify` (installed globally via `pipx install
graphifyy`, not part of this project) was used to build a queryable
knowledge graph of this repo — `graphify-out/` (gitignored, regenerable via
`graphify . --update`). It's genuinely useful for broad "how does X relate
to Y" / "why does this exist" questions (confirmed: tracing why
`BetExecutor` bridges the design-rationale, planning-docs, and
orchestration communities gave an accurate, well-cited answer pulled
straight from the graph's edges) — not a substitute for reading actual
current file content when precision matters (it's a snapshot, goes stale
the moment files change). Global skill file:
`~/.claude/skills/graphify/SKILL.md`. Use `/graphify .` to update it if
you use it and it's gone stale.

## CDP operational notes (unchanged, still true)

- `http://127.0.0.1:9222` is the CDP endpoint, already logged into
  1xbet.cm — never attempt a new login, never launch a new browser.
- League id `2860561`, path segment `2860561-fc-25-3x3-conference-league`.
- `BetExecutor` reads auth (bearer JWT + device token) fresh from the
  browser's cookies/localStorage before every bet via a read-only CDP
  touch — it never drives the betting UI. Two real failure modes seen live
  so far, both already handled as ordinary `BetFailed`s, not crashes: "no
  1xbet.cm tab open" (browser tab closed) and a CDP connect timeout
  (browser unresponsive/busy).

## Constraints that still apply

- Bets go live from the first run for both patterns — no dry-run gate
  (explicit prior product decision).
- Stale-fire guard applies per-pattern via each `TargetTracker`'s
  `stale_statuses` (Pattern 1: `{"half_time", "finished"}`; Pattern 2:
  `{"finished"}` only, since half-time is when its market opens).
- This is real money on the user's real account — treat every new *kind*
  of live action as needing explicit confirmation, same standing rule as
  before. That said, per point 2 above, the user has shown they'll act
  directly on this system outside any given session (manual git push,
  manual restart) — don't assume a live checkpoint you're holding open is
  the only path to a state change; check actual live state before
  reporting on it, don't rely on what you last left it as.
