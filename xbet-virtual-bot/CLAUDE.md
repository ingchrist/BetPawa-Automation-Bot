# CLAUDE.md — Betting patterns on 1xbet virtual 3x3 FIFA (3 patterns + automatic session recovery, all live)

This file exists so a fresh session can pick up this feature without
re-deriving context. It is scoped to this feature, not a general project
guide — see `README.md` for the bot's overall architecture
(collector/aggregator/bettor/display, event bus, config conventions).

**If you're picking this up cold: read this file fully before touching
anything.** This feature places real money automatically. Three betting
patterns and automatic session recovery are all implemented and live.
Check `./run.sh status` and `tail logs/bettor.log` before assuming
anything about current live state, since both go stale the moment anyone
stops/restarts the bot, logs in/out of the browser, or a new round
finishes.

## All patterns + automatic session recovery are live

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
- **Automatic session recovery** — a background watchdog
  (`services/bettor/session_watchdog.py`) that detects when the
  browser's 1xbet.cm session has died and logs back in automatically, so
  a human only needs to log in by hand when automated recovery itself
  can't succeed. Shipped and gone live 2026-09-14,
  `AUTH_WATCHDOG_ENABLED=true` by default. Full 6-task SDD execution,
  including three rounds of real bugs only catchable via actual live
  login attempts (all found via the human-supervised live test each
  fix required, per the plan's own constraint): a duplicate-DOM-id
  selector collision, a login-trigger button that turned out to be a
  toggle (clicking it while already open closed it instead — would have
  made every retry after a real failure fail identically forever), and
  the real root cause behind an "Incorrect username or password!"
  rejection that survived two credential-testing attempts — the account
  is phone-registered, but `login()` had always typed into the site's
  generic "E-mail or ID" field instead of its separate dedicated
  phone-login widget (own toggle icon, own `#phone`/`#phone-password`
  fields, that toggle itself non-idempotent the same way the outer one
  was). See
  `.superpowers/sdd/2026-09-13-bettor-session-auto-relogin/progress.md`
  for the full history if you need it; it's no longer active work.

See `README.md`'s "Betting patterns" and "Automatic session recovery"
sections for the full mechanics of each — that's the maintained, current
reference; don't duplicate pattern math or watchdog behavior here.

## Known, deliberately-not-fixed issues (still true)

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
- In `services/bettor/main.py`'s wiring: no explicit type annotation on
  `watchdog_task`; neither `consumer_task` nor `watchdog_task`'s
  cancellation is awaited before shutdown proceeds (pre-existing
  behavior for `consumer_task`, faithfully extended to `watchdog_task`,
  not a new defect).
- A real, unexplained `400 Bad Request` from `MakeBetWeb` hit a live
  Pattern 3 bet on 2026-09-13 at 22:00:53, while the session was alive
  (not an auth issue). Not investigated — flagged for whoever picks it
  up next.

## Diagnostic scripts in `scratch/`

Gitignored, regenerable/throwaway. Notable ones as of 2026-09-14:
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
since it was last built (the session-auto-relogin feature landed since
the last known build).

## CDP operational notes

- `http://127.0.0.1:9222` is the CDP endpoint, already logged into
  1xbet.cm (when the account is actually logged in — check
  `is_session_alive()` / `./run.sh status` / `tail logs/bettor.log`
  fresh, don't assume) — never attempt a new login via a new
  browser/tab, never launch a new browser.
- League id `2860561`, path segment `2860561-fc-25-3x3-conference-league`.
- `BetExecutor` reads auth (bearer JWT + device token) fresh from the
  browser's cookies/localStorage before every bet via a read-only CDP
  touch — it never drives the betting UI. Two real failure modes seen
  live, both handled as ordinary `BetFailed`s, not crashes: "no
  1xbet.cm tab open" and a CDP connect timeout.
- `services/bettor/session_watchdog.py` is the one thing in this
  codebase that actually drives UI (clicks, types) via CDP against the
  live site, and only for login recovery — bet placement itself remains
  the read-only API-call approach it's always been. Login goes through
  the real browser (not a crafted API POST) because the real login
  endpoint (`/web-api/user/auth`) carries a large client-side-generated
  device fingerprint only the site's own page JS can produce. See that
  module's docstring and constants block for the current selector shape
  (login trigger, phone-widget toggle, `#phone`/`#phone-password`) and
  why each guard exists — the site's own DOM has non-obvious sharp edges
  (duplicate ids, non-idempotent toggle buttons) that were only found by
  live testing, not static inspection.
- The watchdog retries on a cooldown (`AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS`,
  default 300s) if login fails, logging `AUTH LOGIN FAILED` — that means
  automated recovery itself couldn't succeed and a human should log in
  by hand. `AUTH_WATCHDOG_ENABLED=false` in `.env` is the kill switch if
  it ever needs to be silenced (e.g. while investigating a new site
  change) without touching Patterns 1/2/3.

## Constraints that still apply

- Bets go live from the first run for all three patterns — no dry-run
  gate (explicit prior product decision).
- This is real money on the user's real account — treat every new *kind*
  of live action as needing explicit confirmation. The user has
  repeatedly shown they'll act directly on this system outside any given
  session (manual git push, manual restart, manual login/logout during
  live testing, manually editing `.env`) — don't assume a live checkpoint
  you're holding open is the only path to a state change; check actual
  live state before reporting on it, don't rely on what you last left it
  as.
- A live login attempt against the real account is inherently
  rate-limit-risking (the design spec's own accepted residual risk) —
  the automatic watchdog already throttles itself via
  `AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS`, but a manual/ad-hoc login test
  outside that loop (e.g. while debugging a future site change) should
  still be treated as a supervised, human-watched action, not run
  repeatedly on a hunch.
