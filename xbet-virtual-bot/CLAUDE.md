# CLAUDE.md — Pattern 1 betting ("1st Half Over 6.5" streak): ready for Task 9

This file exists so a fresh session can pick up this specific feature
without re-deriving context. It is scoped to that feature, not a general
project guide — see `README.md` for the bot's overall architecture
(collector/aggregator/bettor/display, event bus, config conventions).

**If you're picking this up cold: read this file fully before touching
anything.** This feature places real money automatically — get the state
right before acting.

## What's being built

A `services/bettor/` service, wired like the existing `collector`/
`aggregator`/`display` trio (subscribes to `xbet.match_events` over Redis).
It detects a pattern (3 consecutive finished rounds with 1st-half combined
goal total ≤ 6) and places a real bet on the next round's `Total. 1st half`
market, `Over 6.5`, for a configurable stake (default 90 FCFA).

- **Design spec:** `docs/superpowers/specs/2026-09-09-first-half-over-pattern-bettor-design.md`
- **Implementation plan:** `docs/superpowers/plans/2026-09-09-first-half-over-pattern-bettor.md`
- **SDD execution ledger:** `../.superpowers/sdd/2026-09-09-first-half-over-pattern-bettor/progress.md`
  (this is one level ABOVE this directory — the git repo root is
  `BetPawa-Automation-Bot`, and `xbet-virtual-bot` is a subdirectory of it)
  — this ledger has the authoritative task-by-task log, commit ranges, and
  every ruling made so far. Read it before re-deriving what's done — it's
  more detailed than this file on *why* things ended up the way they did.

Was executed via the `superpowers:subagent-driven-development` skill for
Tasks 1-4; Tasks 5-9 were done inline by the primary session instead, per
a recorded ruling — any task touching the live CDP browser / real betting
gets blocked from subagent dispatch by Claude Code's auto-mode safety
classifier (flags real-money + browser-automation work).

## Status as of this writing

**Tasks 1-8 are complete, committed, and verified.** Branch
`xbet-virtual-bot`, commits (newest first):

```
b21ff8d Record newly finished rounds in data/result.log
5f5a09c Wire the bettor service into run.sh and document Pattern 1 in README   (Task 8)
86d2bb7 Render the betting-pattern events and log them to data/bets.log        (Task 7)
ab4f143 Wire up services/bettor: subscribe, detect, target, bet, publish       (Task 6)
8d81f0d Add BetExecutor: API-based bet placement (reworked from UI-click)      (Task 5)
6d3b9b7 Add TargetTracker: next-match resolution + stale-fire guard            (Task 4)
e2054ec Add PatternTracker: pure streak state machine                         (Task 3)
3007c50 Add config knobs for the streak betting pattern                       (Task 2)
6778465 Add PatternArmed/BetPlaced/BetFailed/BetSettled event kinds           (Task 1)
```

**Only Task 9 (end-to-end live trial) remains** — not a code change, the
final verification step before leaving the bot running unattended. See the
plan doc's Task 9 section for the exact steps (run `./run.sh restart`,
watch `logs/bettor.log` + the terminal for several finished rounds, confirm
the streak counts correctly, confirm a real fire places/settles a bet
correctly if one happens during the trial).

**Before running Task 9, re-read this file's "Constraints" section below**
— Task 9 is the point where the full pipeline runs unattended, so the
guards below matter more here than at any earlier task.

## How `BetExecutor` actually works (Task 5's final, committed design)

The original plan called for driving the betting UI directly with
Playwright clicks (navigate → click "1st half" tab → click "Over 6.5" →
fill stake → click confirm). That was tried live and abandoned mid-task —
too flaky to trust with real money (Vue-SPA re-render races on the tab
click, `connect_over_cdp` hanging on orphaned tabs). The shipped design
(`services/bettor/betting_api.py`) instead places bets via a direct
authenticated API call, matching the same philosophy
`services/collector/xbet_client.py` already uses for reads:

- `POST /service-api/LiveBet/Secure/MakeBetWeb` places the bet. The exact
  request shape was reverse-engineered by capturing one real, authorized
  bet (90 FCFA, Total. 1st half, Over 6.5) via a **raw CDP** network
  monitor — not Playwright. That distinction matters: a first attempt used
  a second, independently-connected Playwright `connect_over_cdp` session
  as a passive observer, and it was verified (reproduced twice) to
  silently receive **zero** Network events triggered by another session's
  activity on an already-open tab, even with a fresh attach and no
  intervening navigation. A direct websocket connection to the tab's own
  `webSocketDebuggerUrl` doesn't have that gap. If a future task ever needs
  to "watch this tab from a second process" again, use raw CDP, not a
  second Playwright session.
- Auth rides on two headers read fresh from the browser before every bet
  (not cached — both expire on a ~4h window): `x-auth` is byte-identical to
  the `access_token` cookie; `x-hd` is byte-identical to the `.token` field
  inside `localStorage["fp_d"]`. Sourced via `read_auth_from_browser()`, a
  read-only CDP touch (cookies + one `page.evaluate`, no navigation, no new
  tab).
- The current coefficient is fetched fresh from `GetGameZip` immediately
  before placing, filtered to `T=9` (Over) / `T=10` (Under) / `P=<line>` /
  `G=17`. **`G=17` is confirmed correct** (2026-09-10): a pre-match UI
  screenshot showing "Total. 1st half" Over 6.5 = 1.83 / Under 6.5 = 2.02
  matched the same match's raw feed coefficient-for-coefficient
  (`{T:9,P:6.5,C:1.83,G:17}` / `{T:10,P:6.5,C:2.02,G:17}`). Earlier
  same-session sampling had shown `G=17` sitting at much higher lines
  (12.5-20.5+) on other matches and that was briefly mistaken for a
  different, full-match market — it's real game state, not a wrong filter:
  these 3x3 virtual matches are high-scoring enough that a single half's
  own total climbs past 12 within the first couple of minutes.
- **Verified twice against the real API**, both real money: the original
  capture bet, and a second real bet placed through this exact client
  (`place_bet(match_id=751525023, home="Red Bull", away="Chelsea",
  stake=90, line=6.5)` → `BetResult(success=True, odds=1.69)`) resolving
  its own match/coefficient rather than replaying the capture's shape.
- Unit-tested (`tests/test_betting_api.py`, 7 tests, offline via
  `httpx.MockTransport` + a fake auth reader) — unlike the original
  UI-driven design, which the spec called "not meaningfully unit-testable."

Full derivation and the exact captured request/response shapes are in
`betting_api.py`'s module docstring — that's the canonical source, this
section is a summary.

## Constraints that still apply (unchanged from the plan's Global Constraints)

- Pattern: 3 consecutive rounds ≤ `PATTERN_LOW_THRESHOLD` (default 6) fires
  a bet on `Over PATTERN_BET_LINE` (default 6.5) for `BET_STAKE_AMOUNT`
  (default 90). After a fire, skip exactly one round before recounting.
- Bets go live from the first run — no dry-run gate (explicit user
  decision from the design phase).
- Stale-fire guard: if the target's 1st half is already over by the time
  the bet is attempted, `BetExecutor` finds no matching odds entry and
  returns `BetResult(success=False, reason="market not open (stale-fire
  guard)")` rather than betting into a closed market.
- This is real money on the user's real account — treat every new kind of
  live action (not just "run the finished bot") as needing explicit
  confirmation, the way both real bets this session did. Task 9 running
  the bot unattended for real, over multiple live rounds, is itself one of
  those — confirm with the user before starting it, and again before
  walking away and leaving it running unattended afterward.

## CDP operational notes (learned the hard way, still true)

- `http://127.0.0.1:9222` is the CDP endpoint, already logged into
  1xbet.cm — never attempt a new login, never launch a new browser.
- **Orphaned tabs accumulate** from any script that opens
  `context.new_page()` and crashes before `page.close()`. Before any live
  CDP work, check
  `curl -s http://127.0.0.1:9222/json/list | python3 -c "import json,sys; print(len(json.load(sys.stdin)))"`
  — expect ~6-8 baseline (the operator's own tabs) plus a handful of
  `service_worker` targets (1xbet.cm re-registers these on navigation;
  they are not page tabs and don't need cleanup). If actual `"type":
  "page"` targets accumulate beyond baseline, close the stale
  `.../esports/virtual...` ones via `curl -s
  http://127.0.0.1:9222/json/close/<id>` before connecting.
- **`pkill -f <pattern>` / `kill $(pgrep -f <pattern>)` is dangerous inside
  a combined shell command that also contains `<pattern>` as literal
  text** (e.g. the command being used to *start* the very script named
  `<pattern>`) — the pattern matches the wrapping shell's own command line
  and kills it before the intended target ever runs. Hit this twice this
  session. Always kill by exact PID, or as a fully separate command with
  no shared substring with whatever you're about to start next.
- A long-running Chrome renderer process (likely the operator's own
  long-open `esports/virtual` tab) was observed growing from ~1GB to
  ~1.8GB RSS over a few hours and triggering the harness's low-memory
  watchdog, which killed unrelated legitimate background jobs (a pure
  Python/httpx logger with no browser involvement). Not something to fix
  unilaterally — never touch the operator's own browser tabs without
  asking — but if background jobs keep getting killed for no obvious
  reason, check `free -h` and prefer quick one-shot foreground commands
  over persistent background processes until memory pressure clears.
- League id `2860561`, URL path segment `2860561-fc-25-3x3-conference-league`.
  Live 3x3 matches are found via
  `a[href*='2860561-fc-25-3x3-conference-league'][href*='/live/']` on
  `https://1xbet.cm/en/esports/virtual/fifa`.
- On a match page: period tabs are "Main game"/"1st half"/"2nd half" — the
  "1st half" tab **disappears entirely once that half ends** (only "Main
  game"/"2nd half" remain), confirmed by direct observation. Halves are
  short (~150s observed, sometimes longer) and these matches are
  high-scoring (8-9+ goals within a single half is common), so anything
  time-sensitive here needs to move fast or poll continuously rather than
  react after the fact.
- Automating the "1st half" tab click was never made reliable this
  session, even freshly, even with the documented 8-10 retry pattern —
  Playwright repeatedly reported the target locator as "resolved to
  visible" and then still timed out on click or on the subsequent heading
  wait. If a future task needs live UI ground-truth again, it may be
  faster to ask the operator to navigate and screenshot manually (as
  happened for the G=17 cross-check) than to keep fighting this in
  automation — not always a "flakiness will pay off if you retry more"
  situation.
