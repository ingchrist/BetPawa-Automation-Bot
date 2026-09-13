# CLAUDE.md — Betting patterns on 1xbet virtual 3x3 FIFA (3 patterns live) + automatic session recovery (in progress, paused)

This file exists so a fresh session can pick up this feature without
re-deriving context. It is scoped to this feature, not a general project
guide — see `README.md` for the bot's overall architecture
(collector/aggregator/bettor/display, event bus, config conventions).

**If you're picking this up cold: read this file fully before touching
anything.** This feature places real money automatically. Three betting
patterns are implemented and live. A fourth piece of work — automatic
session recovery — is **mid-implementation, paused mid-Task-6 at the
user's own request** (not blocked by any technical failure requiring
your intervention first). Get the state right before acting; check
`./run.sh status` and `tail logs/bettor.log` before assuming anything
about current live state, since both go stale the moment anyone
stops/restarts the bot, logs in/out of the browser, or a new round
finishes.

## TL;DR for a fresh session — read this section first

1. **Automatic session recovery is mid-flight, paused, not committed to
   being "done."** Source of truth for exactly where it stands:
   - `.superpowers/sdd/2026-09-13-bettor-session-auto-relogin/progress.md`
     — the execution ledger. **Read this before anything else** — it has
     the full blow-by-blow of what's been tried, what failed, and why.
   - `docs/superpowers/specs/2026-09-13-bettor-session-auto-relogin-design.md`
     — the design.
   - `docs/superpowers/plans/2026-09-13-bettor-session-auto-relogin.md`
     — the 6-task implementation plan.
2. **Where it actually stands:** Tasks 1-5 (the `session_watchdog.py`
   module, its config knobs, wiring into `services/bettor/main.py`,
   README docs) are complete, individually reviewed clean, and committed.
   Task 6 (final regression + one supervised live login test) is where
   things paused. Two real automation bugs were found via live testing
   and fixed (both reviewed, approved, committed):
   - A duplicate-DOM-id selector collision (`#username`/
     `#username-password` each matched a non-input wrapper `<div>` before
     the real `<input>`; fixed by tag-qualifying the selectors).
   - The login trigger button turned out to be a toggle — clicking it
     while the dropdown was already open closed it instead of opening
     it, which would have made every retry after any real failure fail
     the same way forever. Fixed with an idempotent open-guard.
   A third live attempt then ran the automation cleanly end-to-end (no
   exception) but the **site itself rejected the credentials**:
   `"Incorrect username or password!"`. This is not an automation bug.
   Untested hypothesis for next session: `ONEXBET_PHONE_NUMBER` may need
   a country-code prefix (e.g. `237673112163`) rather than just the
   national number — the site's own remembered-login autofill preview
   was seen displaying the number with a `+237` prefix during this
   session's earlier DOM investigation, but the automation currently
   sends the national number alone. Could also simply be a wrong
   password. Not established which — check with the user before
   retrying, don't guess.
3. **Do not immediately retry more login attempts on resuming.** The
   design spec's own accepted residual risk explicitly warns that
   repeated failed logins could draw the site's own anti-bot/rate-limiting
   attention. Three live attempts already happened in the paused session
   (2 automation-bug failures, 1 genuine wrong-credentials rejection).
   Confirm the phone-number-format hypothesis (or get corrected
   credentials) with the user first.
4. **Live-state note as of the pause (2026-09-13, verify fresh, don't
   trust this):** the account was logged OUT when the session paused —
   confirmed, because the still-running pre-watchdog bettor process (the
   live bot does not yet have this feature deployed; Task 6 Step 4,
   restarting onto the new code, hasn't happened yet, gated on Step 3
   actually succeeding first) missed a real Pattern 1 bet at 16:10:15
   with `auth read failed: no access_token cookie`. The user was asked
   to log back in manually before stepping away so Pattern 1/2/3 keep
   working while this feature remains unfinished — check whether they
   did, don't assume either way.
5. **Credentials**: `ONEXBET_PHONE_NUMBER`/`ONEXBET_PASSWORD` are in
   `.env` (gitignored, not in this file, not in git history — keep it
   that way). The values currently there were rejected by the site once;
   don't trust them are correct without checking with the user.
6. **Once this feature actually lands** (Task 6 completes, final
   whole-branch review runs, findings addressed): update this TL;DR
   section the same way Pattern 3's live-fire was documented below, and
   fold the "how it works" summary into the "Both/all patterns' mechanics"
   section as a fourth entry (it's not a pattern, but the same
   fresh-session-context principle applies).

## All three betting patterns are live

- **Pattern 1** — "1st Half Over 6.5" streak. Live since early in this
  feature's history.
- **Pattern 2** — "2nd Half Under 7.5" streak. Live, but currently
  **paused via `.env`'s `PATTERN2_ENABLED=false`** (set 2026-09-11 on
  request — keeps tracking/logging, does not bet; flip back to `true` or
  delete the line to resume).
- **Pattern 3** — "1st Half Winner 2X" streak (Double Chance market, not
  Totals). Shipped and gone live 2026-09-13, `PATTERN3_ENABLED=true` by
  default. Full 8-task SDD execution (including recovering from a
  mid-task machine crash, and catching+fixing a real settlement-bug
  where a Double-Chance-1X win via a draw was being misreported as a
  loss) — see `.superpowers/sdd/2026-09-13-first-half-winner-2x-streak-pattern-bettor/progress.md`
  if you need that history; it's no longer active work.

See `README.md`'s "Betting patterns" section for the full mechanics of
each — that's the maintained, current reference; don't duplicate pattern
math here.

## Known, deliberately-not-fixed issues (from Patterns 1-3, still true)

- `PATTERN1_NAME`/`PATTERN2_NAME` constants (in `services/bettor/main.py`)
  bake the configured bet line into the name — if the corresponding
  `*_BET_LINE` env vars are ever overridden, the name silently desyncs
  from the actual line.
- `BetExecutor.place_bet()`'s `period` branching isn't runtime-validated
  against `{1, 2}`.
- Pattern 1 has a **fixed, undocumented-until-recently priority** over
  Pattern 3 on a same-round mutual-exclusion collision (Pattern 1's
  `MatchHalfTime` handling runs first in `consume()`'s source order) —
  now documented in README, not actually changed. Whether to make this
  fairer is a real open product decision, not yet made.
- Two mutual-exclusion tests the Pattern 3 design spec's own Testing
  section mandated were missing until a final-review fix wave added them
  (`tests/test_targeting.py`) — done, no longer outstanding.

## Diagnostic scripts in `scratch/`

Gitignored, regenerable/throwaway. Notable ones as of 2026-09-13:
- `raw_cdp_monitor.py` — the original passive network-capture technique
  (raw CDP websocket, not Playwright, because a second Playwright
  `connect_over_cdp` session silently misses Network events triggered by
  another session's actions on the same tab). Used to reverse-engineer
  `MakeBetWeb` (bet placement).
- `capture_login.py` — the same technique, adapted to capture the login
  flow (`POST /web-api/user/auth`) for the session-auto-relogin feature.
  Captured data itself (containing encoded credentials/tokens) was
  deleted after use; the reusable script was kept.

## A knowledge graph exists for this codebase (graphify)

A third-party tool called `graphify` (installed globally via `pipx install
graphifyy`, not part of this project) was used to build a queryable
knowledge graph of this repo — `graphify-out/` (gitignored, regenerable via
`graphify . --update`). Genuinely useful for broad "how does X relate to
Y" / "why does this exist" questions — not a substitute for reading
actual current file content when precision matters. Global skill file:
`~/.claude/skills/graphify/SKILL.md`. Use `/graphify .` to update it if
you use it and it's gone stale — it will be, given how much has changed
since it was last built.

## CDP operational notes (unchanged, still true)

- `http://127.0.0.1:9222` is the CDP endpoint, already logged into
  1xbet.cm (when the account is actually logged in — see the session-
  auto-relogin TL;DR above for why that's not a safe assumption right
  now) — never attempt a new login via a new browser/tab, never launch a
  new browser.
- League id `2860561`, path segment `2860561-fc-25-3x3-conference-league`.
- `BetExecutor` reads auth (bearer JWT + device token) fresh from the
  browser's cookies/localStorage before every bet via a read-only CDP
  touch — it never drives the betting UI. Two real failure modes seen
  live, both handled as ordinary `BetFailed`s, not crashes: "no
  1xbet.cm tab open" and a CDP connect timeout.
- The session-auto-relogin feature (in progress) is the **first** thing
  in this codebase that actually drives UI (clicks, types) via CDP
  against the live site, and only for login recovery — bet placement
  itself remains the read-only API-call approach it's always been. See
  `services/bettor/session_watchdog.py`'s module docstring once that
  file exists (it does, as of Task 1).

## Constraints that still apply

- Bets go live from the first run for all three patterns — no dry-run
  gate (explicit prior product decision).
- This is real money on the user's real account — treat every new *kind*
  of live action as needing explicit confirmation. The user has
  repeatedly shown they'll act directly on this system outside any given
  session (manual git push, manual restart, manual login/logout during
  live testing) — don't assume a live checkpoint you're holding open is
  the only path to a state change; check actual live state before
  reporting on it, don't rely on what you last left it as.
- A live login attempt against the real account (part of the session-
  auto-relogin feature's own verification) is explicitly a
  human-supervised action in that plan's Global Constraints — never run
  it unattended, and mind the repeated-attempt/rate-limiting risk noted
  above.
