# xbet-virtual-bot

Watches 1xbet.cm's FIFA 3x3 virtual esports (currently "FC 25. 3x3.
Conference League") and prints scores — live and upcoming — to the terminal
in real time. Also runs one live betting pattern (`services/bettor/`) that
places real bets automatically when its trigger condition fires — see
[Betting patterns](#betting-patterns).

## Contents

- [Quick start](#quick-start)
- [Example output](#example-output)
- [Architecture](#architecture)
- [Event catalogue](#event-catalogue)
- [Betting patterns](#betting-patterns)
- [How data is sourced](#how-data-is-sourced)
- [Configuration reference](#configuration-reference)
- [Project layout](#project-layout)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)

## Quick start

Requires `redis-server` on PATH and Python 3.12+ (both already present on
this box).

```
./run.sh            # installs deps into .venv on first run, starts everything
```

This starts `collector`, `aggregator`, and `bettor` in the background (logs
under `logs/`) and attaches `display` to your terminal. Ctrl+C stops
*watching* — the background services keep running (no state is lost) so you
can reattach with `./run.sh` again, or `.venv/bin/python -m
services.display.main` directly. `bettor` places real bets automatically
once its pattern fires — see [Betting patterns](#betting-patterns) before
running this for the first time.

```
./run.sh status      # what's running
./run.sh stop        # stop collector + aggregator + bettor
./run.sh restart
```

Config knobs (poll intervals, the league filter, Redis URL, ...) live in
`.env` — copy `.env.example` to start; every value has a working default so
an empty `.env` is fine. Full reference: [Configuration
reference](#configuration-reference).

## Example output

Captured from a real run (team names are illustrative — whatever's actually
on in the target league prints the same way). Layout follows a reference
design: a "Result" grid — goals per half for each team, plus the *combined*
both-teams goal total for each half and for the match overall — with the
elapsed match time underneath, rather than the odds-ladder box this used to
sit next to:

```
Legend: UPCOMING = the next match to kick off (no score yet) · ● live update, with the Result grid
and elapsed time so far · RESULT = final score. ✓ marks the settled winning side; scroll up for
earlier results.

— now watching live —

╭─ UPCOMING  FC 25. 3x3. Conference League  ·  Borussia Monchengladbach vs Lille ──────────────────╮
│ Kickoff: Starting in 13 minutes                                                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
● KICK-OFF  FC 25. 3x3. Conference League  ·  Chelsea vs Anderlecht
╭─ ● LIVE  FC 25. 3x3. Conference League  ·  Chelsea vs Anderlecht ────────────────────────────────╮
│ Chelsea  4 : 4  Anderlecht                                                                       │
│                                                                                                  │
│ ┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━┓                       │
│ ┃            ┃          ┃ total for ┃          ┃ total for ┃             ┃                       │
│ ┃ Result     ┃ 1st half ┃ 1st half  ┃ 2nd half ┃ 2nd half  ┃ final total ┃                       │
│ ┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━┩                       │
│ │ Chelsea    │        4 │     8     │        – │           │             │                       │
│ │ Anderlecht │        4 │           │        – │           │      8      │                       │
│ └────────────┴──────────┴───────────┴──────────┴───────────┴─────────────┘                       │
│                                  time elapse   1st half · 02:30                                  │
╰──────────────────────────────────────────────────────────────────────────────────────────────────╯
● HALF-TIME  FC 25. 3x3. Conference League  ·  Chelsea 6 - 4 Anderlecht
╭─ RESULT  FC 25. 3x3. Conference League  ·  Chelsea vs Anderlecht ────────────────────────────────╮
│ ┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━┓                       │
│ ┃            ┃          ┃ total for ┃          ┃ total for ┃             ┃                       │
│ ┃ Result     ┃ 1st half ┃ 1st half  ┃ 2nd half ┃ 2nd half  ┃ final total ┃                       │
│ ┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━┩                       │
│ │ Chelsea    │        6 │    10     │        2 │           │             │                       │
│ │ Anderlecht │        4 │           │        1 │     3     │     13      │                       │
│ └────────────┴──────────┴───────────┴──────────┴───────────┴─────────────┘                       │
│ W1 1.97 ✓  X 7.60  W2 2.30                                                                       │
╰─────────────────────────────────────────────────────────────────────────────────────── 13 goals ─╯
```

In the real terminal, `UPCOMING` panels border yellow, `●` live lines and the
`RESULT` panel border green/cyan, and `✓` / the losing side of a settled
line render in bold-green / dim. Nothing here ever redraws in place — every
block is appended to normal scrollback, so scrolling up shows the entire
history of the session, in order, exactly as it happened.

Two rules behind this layout, both deliberate:

- **No odds-price ladder in the terminal.** The "total for 1st/2nd half" and
  "final total" columns are the actual, settled goal counts — the number
  that answers an Over/Under question directly, without a price table next
  to it that only ever changed on a handful of the many goal events it used
  to be reprinted on.
- **Only one upcoming match is ever shown** — the very next one to kick
  off, not the whole queue. This league runs several matches back to back,
  each with its own countdown, and the site exposes all of them at once;
  without this, every discovered match got its own panel immediately,
  which is what made the earlier layout feel like it was "occupying too
  much space." See [Architecture](#architecture) for how the one-at-a-time
  pick works.

## Architecture

Four independent processes talking only through Redis pub/sub — kill,
restart, or swap any one of them and the others don't notice:

```
 1xbet.cm JSON API
        │
        ▼
  ┌─────────────┐  MatchSnapshot   ┌──────────────┐  MatchEvent    ┌─────────────┐
  │  collector  │ ───────────────► │  aggregator  │ ─────────────► │   display   │
  └─────────────┘   (xbet.snap-    └──────────────┘  (xbet.match_  └─────────────┘
   polls the API      shots)        state machine:     events)      terminal UI,
   for the target                   upcoming→live→                  rich panels,
   league, no                       finished; settles                replays
   memory of what                   odds; appends                    history on
   changed                          data/results.jsonl                startup
```

- **collector** (`services/collector/`) — the only service that talks to
  1xbet. Polls the bulk live feed for the target league, enriches each match
  with odds via a throttled per-match detail call, normalizes into
  `MatchSnapshot`, publishes. No judgment about what's "new" — just current
  state.
- **aggregator** (`services/aggregator/`) — the only service that assigns
  meaning. Diffs each incoming snapshot against the last one for that match
  and emits `MatchDiscovered` / `MatchStarted` / `MatchScoreChanged` /
  `MatchHalfTime` / `MatchFinished` (see [Event
  catalogue](#event-catalogue)). Settles odds (marks the winning side) and
  appends every finished result to `data/results.jsonl`.
  It also owns two pieces of judgment beyond simple diffing:
  - **Picking the single "next" upcoming match.** This league queues
    several matches at once; the aggregator tracks every one it currently
    knows about but only ever announces (emits `MatchDiscovered` for) the
    soonest-kickoff one — re-decided on every update rather than fixed at
    first sight, because these matches' real durations vary widely (~10s to
    6+ minutes observed), which reshuffles the queue behind them. See
    `_update_upcoming_queue` in `services/aggregator/state.py`.
  - **Filtering out stale upstream reads.** The feed occasionally serves a
    briefly-inconsistent read — a live match reported back as upcoming for
    one poll, or a cumulative goal total dipping before immediately
    recovering — both impossible in a self-consistent feed. `_is_stale_reading`
    drops these outright rather than let them become duplicate KICK-OFF
    lines or a goal that appears to un-score itself.
- **bettor** (`services/bettor/`) — subscribes to `xbet.match_events` like
  display does, but also publishes back onto it: feeds every finished
  round's 1st-half total into a pure streak-detector (`pattern.py`),
  resolves which upcoming match a fired pattern bets on (`targeting.py`),
  and places the bet via a direct API call (`betting_api.py`), publishing
  `PatternArmed`/`BetPlaced`/`BetFailed`/`BetSettled` back onto the same
  channel — see [Betting patterns](#betting-patterns).
- **display** (`services/display/`) — a dumb renderer. Subscribes to domain
  events, prints a block per event via `rich`. Replays the last few results
  from `data/results.jsonl` on startup before switching to live streaming,
  so a restart doesn't lose "what happened earlier" — the live site itself
  drops that the moment a match scrolls off its own live list. It also
  appends every newly-finished result's RESULT block, as plain text, to
  `data/result.log` — see [the note in Project
  layout](#project-layout) for why this is a separate file from
  `data/results.jsonl`. It renders bettor's four event kinds too, logging
  them to `data/bets.log` the same way.

`shared/` holds what all four agree on: the event schemas
(`events.py`, pydantic — this is the real contract between the services),
the Redis wrapper (`bus.py`), and config (`config.py`). Because that
contract is explicit and typed, bettor itself was added this way — just
another subscriber to `xbet.match_events` — and a further service (a
Telegram notifier, a DB writer, a web dashboard, another betting pattern)
costs nothing to add the same way, without destabilizing anything upstream.

Every service is independently restartable via its own `if __name__ ==
"__main__"` entrypoint (`python -m services.<name>.main`) and logs to both
`logs/<name>.log` (persistent, for postmortems) and stderr (for watching it
live) — see `shared/logging.py`.

## Event catalogue

Everything the aggregator can say, in the order a single match normally
produces them. Every model lives in `shared/events.py`; `kind` is the
discriminator field used to route a message off the wire (see
`shared/bus.py`).

| Channel | Event (`kind`) | Fired when | Key fields |
|---|---|---|---|
| `xbet.snapshots` | `MatchSnapshot` (`snapshot`) | Every poll, for every match currently in the target league — collector → aggregator only, not usually of interest downstream. | `status` (upcoming/live/finished), `period_label`, `clock_seconds`, `home_goals`/`away_goals`, `half_scores`, `moneyline`, `totals` |
| `xbet.match_events` | `MatchDiscovered` (`discovered`) | The soonest-kickoff match currently known becomes a *different* match than the one last announced — at most one match is ever the "announced next" at a time. See [Architecture](#architecture). | `starting_in_label`, `moneyline`, `totals` (not currently rendered — see [Example output](#example-output)) |
| `xbet.match_events` | `MatchStarted` (`started`) | Status flips to live (or the bot starts watching a match already live/finished — see [Known limitations](#known-limitations)). | — |
| `xbet.match_events` | `MatchScoreChanged` (`score_changed`) | The running score changes while live — one event per goal, essentially. | `period_label`, `clock_seconds`, `home_goals`, `away_goals`, `totals` (carried on the event but not rendered — the terminal shows the actual combined goal total instead, see [Example output](#example-output)) |
| `xbet.match_events` | `MatchHalfTime` (`half_time`) | First-half data becomes available and the match has moved past the first half. Fires exactly once per match — see the note on `_half_time_emitted` in `services/aggregator/state.py` for why it's edge-triggered rather than a literal frame-to-frame comparison. | `first_half` |
| `xbet.match_events` | `MatchFinished` (`finished`) | Status flips to finished. Appended to `data/results.jsonl` (by the aggregator) and `data/result.log` (by the display) in the same step. | `first_half`, `second_half`, `total_home_goals`/`total_away_goals`, settled `moneyline` (rendered), `totals` (settled but not rendered — see [Example output](#example-output)) |
| `xbet.match_events` | `PatternArmed` (`pattern_armed`) | A betting pattern's trigger condition is met. | `pattern_name`, `qualifying_totals` |
| `xbet.match_events` | `BetPlaced` (`bet_placed`) | A bet was successfully placed. | `match_id`, `stake`, `line`, `odds` |
| `xbet.match_events` | `BetFailed` (`bet_failed`) | A bet was skipped or failed. | `match_id?`, `reason` |
| `xbet.match_events` | `BetSettled` (`bet_settled`) | The bet's target match finished. | `match_id`, `won`, `first_half_total` |

All four are published by `services/bettor/` — see [Betting patterns](#betting-patterns).

`MatchStateMachine.process()` (in `services/aggregator/state.py`) always
returns events in the order above for a single poll, even if a match jumps
several states between two polls (a slow poll, a missed frame) — so the
display never prints e.g. a `RESULT` block before the `HALF-TIME` block it
belongs after.

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
mode. Placement (`services/bettor/betting_api.py`) follows the same
philosophy as the read path described in [How data is
sourced](#how-data-is-sourced): a direct authenticated POST to the site's
own `LiveFeed`/`LiveBet` JSON API, reverse-engineered by capturing one
real, authorized bet — not by driving the betting UI with browser clicks
(an earlier design did that, proved too flaky live to trust with real
money, and was abandoned; see the module's docstring for the full story).
It does still need `CDP_URL` (default `http://127.0.0.1:9222`, the same
already-running, already-logged-in Chrome the bot never launches or logs
into itself) for one thing: reading two auth values — a bearer token and a
device/session token — straight out of that browser's cookies and
`localStorage` fresh before every bet, since both expire on a ~4-hour
window. That's a read-only touch, not navigation or clicking, so it
doesn't carry the flakiness the abandoned UI-click design did.

If the target round's 1st half is already over (half-time or finished) by
the moment the bet is actually attempted — real time passes between
detection and placement — the bet is skipped and logged as failed rather
than placed into a stale market.

Every pattern fire / bet placed / bet failed / bet settled is rendered
in the terminal (same panel style as the RESULT grid) and appended to
`data/bets.log` (plain text, tracked in git, same convention as
`data/result.log`) so the pattern's real hit-rate is reviewable over
time.

Config knobs: `BET_STAKE_AMOUNT`, `PATTERN_STREAK_LENGTH`,
`PATTERN_LOW_THRESHOLD`, `PATTERN_BET_LINE`, `CDP_URL`, `BETS_LOG_PATH` —
see `.env.example`.

**Verified end-to-end (2026-09-10)**: beyond the original capture bet (used
to observe the real request shape), a second real bet was placed through
`BetExecutor` itself against a live match/coefficient it resolved on its
own, confirming the client actually places bets correctly rather than just
replaying one captured shape. The odds-ladder field disambiguating "1st
half" markets from other periods was open too, and is now confirmed
correct by directly matching a live UI price against the raw feed — see
`services/bettor/betting_api.py`'s docstring for both.

### Pattern 2 — "2nd Half Under 7.5" streak

The mirror image of Pattern 1, run as a second, independent pattern in
the same `services/bettor/` process — no separate service, no separate
CDP session. It watches every finished round's **2nd-half** combined
goal total instead of the 1st-half's. 3 consecutive rounds at or above
`PATTERN2_HIGH_THRESHOLD` (default 8) fire a real bet —
`PATTERN2_BET_STAKE_AMOUNT` (default 90, FCFA, independently
configurable from Pattern 1's stake) on the *next* round's `Total. 2nd
half` market, `Under PATTERN2_BET_LINE` (default 7.5). Same fire → skip
one round → restart life cycle as Pattern 1, evaluated in the opposite
direction.

Because a final 2nd-half score is only known once a round actually
finishes (unlike the 1st half, whose score is known at half-time),
Pattern 2 evaluates at each round's `MatchFinished` event rather than
`MatchHalfTime` — one event later in the round's lifecycle than
Pattern 1.

**Mutual exclusion:** since both patterns watch the same "next match to
kick off," they can resolve to targeting the same match in the same
round. Only one bet per match is ever placed — whichever pattern's
target resolves first wins it; the other logs a `BET FAILED` with a
`mutual exclusion: ...` reason instead of also staking money on it.

Config knobs: `PATTERN2_HIGH_THRESHOLD`, `PATTERN2_STREAK_LENGTH`,
`PATTERN2_BET_LINE`, `PATTERN2_BET_STAKE_AMOUNT` — see `.env.example`.
Shares `CDP_URL` and `BETS_LOG_PATH` with Pattern 1.

## How data is sourced

The original plan was to drive the shared Chrome-over-CDP setup this box
normally uses (see `/home/cdjinguet/CLAUDE.md`) and scrape the rendered
page. That doesn't work here: **1xbet.cm's HTML routes are geo-blocked** for
this box's egress IP — every page request 302s to `/en/block`
(`x-gw-blk-redirect-reason: block`).

The page's own frontend doesn't hand-render odds, though — it calls a JSON
API at `/service-api/LiveFeed/...` to get them, and *that path is not
behind the same block* (verified directly: `/` 302-blocks, `/service-api/…`
200s, from the same IP). So `services/collector/xbet_client.py` talks to
that API directly instead of a browser. That's arguably the better design
regardless of the geo-block — it's the exact data the page itself uses,
structured, with nothing to break when the site's markup changes.

Only FIFA (`SI=85`) matches whose league name contains "3x3" (case
insensitive, configurable via `ONEXBET_LEAGUE_NAME_FILTER`) are tracked —
this deliberately excludes FIFA 4x4, 5x5 Rush, Penalty shootouts, and every
non-3x3 league. It's a name filter rather than a hardcoded league id because
the site rotates the league every so often (e.g. "FC 25. 3x3. Conference
League" will eventually become "FC 26. 3x3. ..."); the filter keeps working
across that rotation without a code change.

The full endpoint/parameter/odds-market reference — including which query
params the API silently 406-rejects, and the `E[].T` codes for each market
(1/2/3 = W1/X/W2, 9/10 = Total Over/Under) — lives in the docstring at the
top of `services/collector/xbet_client.py`; don't duplicate it here, that's
the one place it should live.

**If the API ever changes** (a field renamed, a 406 starts appearing where
it didn't before): this was reverse-engineered by probing with plain
`curl`/`urllib`, not from any published spec. Re-diagnosing it means the
same thing again — hit `GetChampsZip?lng=en` to re-find the current league's
`LI`, hit `Get1x2_VZip?sports=85&count=100&lng=en&getEmpty=true` to confirm
the bulk shape, hit `GetGameZip?id=<a live match id>&lng=en` and diff its
`E` array against the `T`/`G` codes documented in `xbet_client.py`. The
read path (collector) has no browser/CDP dependency at all — don't
reintroduce one as a "fix" for an API change; the geo-block is still
there. (The betting path does use a lightweight, read-only CDP touch to
source auth tokens — see [Betting patterns](#betting-patterns) — but that's
reading cookies/localStorage from an already-open browser tab, not
scraping rendered HTML, so it isn't affected by the geo-block either way.)

## Configuration reference

Every variable lives in `.env` (copy from `.env.example`); every one has a
working default in `shared/config.py`; `.env` only needs to override what
you change.

| Variable | Default | Meaning |
|---|---|---|
| `ONEXBET_API_BASE` | `https://1xbet.cm/service-api` | Base URL for the JSON API. |
| `ONEXBET_SPORT_ID` | `85` | 1xbet's internal sport id for "FIFA". |
| `ONEXBET_LEAGUE_NAME_FILTER` | `3x3` | Case-insensitive substring match against a league's name. See [How data is sourced](#how-data-is-sourced). |
| `COLLECTOR_BULK_POLL_SECONDS` | `5` | How often the collector re-polls the whole-sport bulk feed. |
| `COLLECTOR_DETAIL_POLL_SECONDS` | `5` | Minimum gap between per-match detail (odds) refetches; never refetched once a match is finalized. |
| `HTTP_TIMEOUT_SECONDS` | `10` | Timeout for every call to the 1xbet API. |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | The event bus. |
| `RESULTS_LOG_PATH` | `data/results.jsonl` | Machine-readable append-only finished-match history, used by `display` to backfill on its own startup (see [Known limitations](#known-limitations)). |
| `RESULT_LOG_PATH` | `data/result.log` | Human-readable twin of the above — the plain-text RESULT block for every finished round, meant to be opened directly. Note the singular "RESULT," easy to confuse with `RESULTS_LOG_PATH` above; they're different files serving different purposes. |
| `LOG_DIR` | `logs` | Per-service log files. |
| `BET_STAKE_AMOUNT` | `90` | FCFA staked per fired bet (Pattern 1). See [Betting patterns](#betting-patterns). |
| `PATTERN_STREAK_LENGTH` | `3` | Consecutive qualifying rounds required to fire. |
| `PATTERN_LOW_THRESHOLD` | `6` | A round qualifies when its 1st-half combined goal total is at or under this. |
| `PATTERN_BET_LINE` | `6.5` | The Over line bet on in `Total. 1st half`. |
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the already-logged-in browser the bettor reads fresh auth from (read-only touch, not UI automation). |
| `BETS_LOG_PATH` | `data/bets.log` | Human-readable audit trail of every pattern fire / bet placed / failed / settled — tracked in git like `RESULT_LOG_PATH`. |

Redis pub/sub channel names (`xbet.snapshots`, `xbet.match_events`) are
**not** env-configurable — they're the fixed contract between services and
live as constants on the `Config` dataclass (`shared/config.py`) precisely
so they can't drift out of sync between a publisher and its subscribers.

## Project layout

```
shared/                event schemas, Redis bus, config, logging — the contract
services/collector/    1xbet API client, raw JSON → MatchSnapshot, poll loop
services/aggregator/   state machine, odds settlement, results history log
services/bettor/       Pattern 1 streak detector + live bet placement
services/display/      rich-rendered terminal blocks, history replay
run.sh                 installs the venv, starts Redis + all four services
data/results.jsonl     machine-readable finished-match log (gitignored)
data/result.log         human-readable RESULT block per finished round (tracked in git — see below)
data/bets.log           human-readable betting audit trail (tracked in git — see Betting patterns)
logs/                  per-service log files (gitignored)
```

`data/results.jsonl` and `data/result.log` are deliberately two different
files rather than one, and are treated differently by `.gitignore`:

- `results.jsonl` is JSON Lines, written by the **aggregator**, and exists
  so the **display** service can backfill recent results into scrollback
  after a restart (see `services/aggregator/history.py`) — it's an
  internal implementation detail of that replay, not meant to be read
  directly. Stays gitignored.
- `result.log` is plain text, written by the **display** service, and
  exists for *you* — the exact RESULT block the terminal prints (Result
  grid, moneyline, ✓ on the winning side), one per finished
  round, timestamped, at a fixed width regardless of terminal size, meant
  to be opened directly for reviewing rounds — e.g. spotting patterns
  across results for predictions. Only genuinely new finishes are
  appended — the startup backfill replay is deliberately not written here,
  or every restart would duplicate however many rounds it replays (see
  `BACKFILL_COUNT` in `services/display/main.py`). **Tracked in git** (see
  the `!data/result.log` exception in `.gitignore`) rather than gitignored
  like everything else this bot generates, since it's the actual data the
  project exists to produce — commit it whenever you want the latest
  rounds captured in history.

## Known limitations

Honest gaps, not hidden ones — worth knowing before relying on this:

- **A restarted `display` doesn't recover in-progress state.** Redis
  pub/sub has no history: a fresh `display` subscriber only sees events
  published *after* it connects. It backfills recent **finished** results
  from `data/results.jsonl` on startup (see `services/display/main.py`),
  but a match that's currently upcoming or live when `display` restarts
  gets no retroactive `UPCOMING` panel — you'll just start seeing its next
  goal/half-time/result. `collector`/`aggregator` staying up across a
  `display` restart avoids this in practice (`./run.sh` only restarts all
  three together via `stop`+`start`). A proper fix would mean the
  aggregator re-announcing current state to new subscribers, or swapping
  pub/sub for Redis Streams — not needed for phase 1.
- **Aggregator state is single-process, in-memory, unbounded.** See the
  docstring on `MatchStateMachine.__init__` in
  `services/aggregator/state.py` — fine for one small league running for
  weeks, would need a TTL or external store to track many leagues over a
  long unbroken run.
- **The API is unofficial, and occasionally briefly inconsistent.** It's
  1xbet's own frontend's API, not a published/versioned one — see [How data
  is sourced](#how-data-is-sourced) for how to re-diagnose it if it changes
  shape. It has also been observed, more than once, serving a stale read a
  poll or two after a fresher one already arrived (the bulk feed and a
  per-match detail call briefly disagreeing, or plain replica lag) —
  `_is_stale_reading` in `services/aggregator/state.py` drops these
  outright rather than let them surface as a duplicate KICK-OFF line or a
  goal that appears to un-score itself. If a *new* class of upstream
  glitch shows up that this doesn't already cover, that function is where
  to extend the guard.
- **No automated tests yet** (for the collector/aggregator/display trio — `services/bettor/` does have unit tests for its pure state machines and its bet-placement client, plus two independent real-money verifications: the original capture bet and a second real bet placed through `BetExecutor` itself resolving its own match/coefficient). Verified so far by running the full stack
  live against the real feed and reading the resulting logs / terminal
  output / `data/results.jsonl` for correctness — this is how every bug
  fixed so far (half-time edge-triggering, duplicate finished events, the
  upstream-inconsistency guard above, the upcoming-queue redesign) was
  actually found; see the explanatory comments in
  `services/aggregator/state.py` for each. Worth adding unit tests for
  `MatchStateMachine` and `normalize.py` before this grows much further;
  they're pure functions / a pure state machine, so they don't need Redis
  or network mocking to test.

## Troubleshooting

- **`detail fetch failed for match <id>: ConnectTimeout('')`** in
  `logs/collector.log` — expected occasionally, not a bug. The collector
  falls back to the bulk-feed snapshot for that match and retries detail on
  the next poll; nothing to act on unless it's constant, in which case
  check general network connectivity to `1xbet.cm`.
- **Nothing prints after startup.** Matches in this league run back-to-back
  but only one plays at a time — there can be a genuine quiet gap. Check
  `logs/aggregator.log` for `discovered`/`started` lines to confirm data is
  flowing; if that's empty too, check `logs/collector.log` for repeated
  `poll error:` lines (network or API-shape problem — see [How data is
  sourced](#how-data-is-sourced)).
- **`redis-cli: command not found` / `./run.sh` fails at "starting local
  redis-server..."** — install `redis-server` (`apt install redis-server`)
  or point `REDIS_URL` at an already-running instance and remove the
  `ensure_redis` call's relevance (i.e. just make sure something answers on
  that URL before starting the services).
- **Thinking about scraping the rendered page instead** (for a future
  phase, e.g. something not exposed via this API) — re-read [How data is
  sourced](#how-data-is-sourced) first: the HTML route is geo-blocked from
  this box, browser/CDP won't work here without a proxy.

## Roadmap

Extraction + real-time terminal display, plus two live betting patterns
(see [Betting patterns](#betting-patterns)), are all implemented. Both
patterns run in the same `services/bettor/` process, coordinated so they
never both bet on the same match in the same round. Any future pattern
follows the same shape: extend `PatternTracker`/`TargetTracker`/
`BetExecutor` rather than forking them, and publish its own
`PatternArmed`/`Bet*` events onto the same `xbet.match_events` bus.
