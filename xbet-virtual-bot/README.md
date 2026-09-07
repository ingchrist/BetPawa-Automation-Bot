# xbet-virtual-bot

Phase 1: watch 1xbet.cm's FIFA 3x3 virtual esports (currently "FC 25. 3x3.
Conference League") and print scores — live and upcoming — to the terminal
in real time. No betting actions yet; that's a later phase.

## Contents

- [Quick start](#quick-start)
- [Example output](#example-output)
- [Architecture](#architecture)
- [Event catalogue](#event-catalogue)
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

This starts `collector` and `aggregator` in the background (logs under
`logs/`) and attaches `display` to your terminal. Ctrl+C stops *watching* —
the background services keep running (no state is lost) so you can reattach
with `./run.sh` again, or `.venv/bin/python -m services.display.main`
directly.

```
./run.sh status      # what's running
./run.sh stop        # stop collector + aggregator
./run.sh restart
```

Config knobs (poll intervals, the league filter, Redis URL, ...) live in
`.env` — copy `.env.example` to start; every value has a working default so
an empty `.env` is fine. Full reference: [Configuration
reference](#configuration-reference).

## Example output

Captured from a real run (team names are illustrative — whatever's actually
on in the target league prints the same way). Layout follows a reference
design: a compact two-box scoreboard (league + score + clock, Total O/U
ladder) rather than the tall single-column table this started with:

```
Legend: UPCOMING = the next match to kick off (no odds yet) · ● live update,
with the current Total O/U ladder · RESULT = final score. ✓ marks the settled
winning side; scroll up for earlier results.

— now watching live —

╭─ UPCOMING  FC 25. 3x3. Conference League  ·  Borussia Monchengladbach vs Lil─╮
│ Kickoff: Starting in 13 minutes                                              │
╰──────────────────────────────────────────────────────────────────────────────╯
● KICK-OFF  FC 25. 3x3. Conference League  ·  Chelsea vs Anderlecht
╭────────────────────────────────────────╮ ╭──────────────────────────────╮
│ FC 25. 3x3. Conference League          │ │ Total                        │
│                                        │ │ O 15.5  1.195  U 15.5  4.08  │
│ Chelsea  4 : 4  Anderlecht             │ │ O 16.5  1.56   U 16.5  2.36  │
│ 1st half · 02:30                       │ │ O 17.5  2.26   U 17.5  1.64  │
╰────────────────────────────────────────╯ │ O 18.5  3.58   U 18.5  1.275 │
                                           ╰──────────────────────────────╯
● HALF-TIME  FC 25. 3x3. Conference League  ·  Chelsea 6 - 4 Anderlecht
╭─ RESULT  FC 25. 3x3. Conference League  ·  Chelsea vs Anderlecht ────────────╮
│             1st half  2nd half  Total                                        │
│ Chelsea            6         2      8                                        │
│ Anderlecht         4         1      5                                        │
│ W1 1.97 ✓  X 7.60  W2 2.30                                                   │
│ Total                                                                        │
│ O 15.5  1.2    U 15.5  4 ✓                                                   │
│ O 12.5  1.4 ✓  U 12.5  3                                                     │
╰─────────────────────────────────────────────────────────────────── 13 goals ─╯
```

In the real terminal, `UPCOMING` panels border yellow, `●` live lines and the
`RESULT` panel border green/cyan, and `✓` / the losing side of a settled
line render in bold-green / dim. Nothing here ever redraws in place — every
block is appended to normal scrollback, so scrolling up shows the entire
history of the session, in order, exactly as it happened.

Two rules behind this layout, both deliberate:

- **Odds only ever appear next to a score.** `UPCOMING` shows just the
  kickoff countdown, nothing else — there's no live score yet for a market
  move to mean anything against. The Total ladder only starts appearing
  once a match is actually live, and stays through `RESULT`.
- **Only one upcoming match is ever shown** — the very next one to kick
  off, not the whole queue. This league runs several matches back to back,
  each with its own countdown, and the site exposes all of them at once;
  without this, every discovered match got its own panel immediately,
  which is what made the earlier layout feel like it was "occupying too
  much space." See [Architecture](#architecture) for how the one-at-a-time
  pick works.

## Architecture

Three independent processes talking only through Redis pub/sub — kill,
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
- **display** (`services/display/`) — a dumb renderer. Subscribes to domain
  events, prints a block per event via `rich`. Replays the last few results
  from `data/results.jsonl` on startup before switching to live streaming,
  so a restart doesn't lose "what happened earlier" — the live site itself
  drops that the moment a match scrolls off its own live list. It also
  appends every newly-finished result's RESULT block, as plain text, to
  `data/result.log` — see [the note in Project
  layout](#project-layout) for why this is a separate file from
  `data/results.jsonl`.

`shared/` holds what all three agree on: the event schemas
(`events.py`, pydantic — this is the real contract between the services),
the Redis wrapper (`bus.py`), and config (`config.py`). Because that
contract is explicit and typed, a fourth service (a Telegram notifier, a DB
writer, a web dashboard) is just another subscriber to `xbet.match_events` —
it costs nothing to add and can't destabilize collector or aggregator.

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
| `xbet.match_events` | `MatchScoreChanged` (`score_changed`) | The running score changes while live — one event per goal, essentially. | `period_label`, `clock_seconds`, `home_goals`, `away_goals`, `totals` (the live Total O/U ladder at this instant) |
| `xbet.match_events` | `MatchHalfTime` (`half_time`) | First-half data becomes available and the match has moved past the first half. Fires exactly once per match — see the note on `_half_time_emitted` in `services/aggregator/state.py` for why it's edge-triggered rather than a literal frame-to-frame comparison. | `first_half` |
| `xbet.match_events` | `MatchFinished` (`finished`) | Status flips to finished. Appended to `data/results.jsonl` (by the aggregator) and `data/result.log` (by the display) in the same step. | `first_half`, `second_half`, `total_home_goals`/`total_away_goals`, settled `moneyline`/`totals` (winning side marked) |

`MatchStateMachine.process()` (in `services/aggregator/state.py`) always
returns events in the order above for a single poll, even if a match jumps
several states between two polls (a slow poll, a missed frame) — so the
display never prints e.g. a `RESULT` block before the `HALF-TIME` block it
belongs after.

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
`E` array against the `T`/`G` codes documented in `xbet_client.py`. There is
no dependency on the browser/CDP setup anywhere in this bot — don't
reintroduce it as a "fix" for an API change; the geo-block is still there.

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

Redis pub/sub channel names (`xbet.snapshots`, `xbet.match_events`) are
**not** env-configurable — they're the fixed contract between services and
live as constants on the `Config` dataclass (`shared/config.py`) precisely
so they can't drift out of sync between a publisher and its subscribers.

## Project layout

```
shared/                event schemas, Redis bus, config, logging — the contract
services/collector/    1xbet API client, raw JSON → MatchSnapshot, poll loop
services/aggregator/   state machine, odds settlement, results history log
services/display/      rich-rendered terminal blocks, history replay
run.sh                 installs the venv, starts Redis + all three services
data/results.jsonl     machine-readable finished-match log (gitignored)
data/result.log         human-readable RESULT block per finished round (gitignored)
logs/                  per-service log files (gitignored)
```

`data/results.jsonl` and `data/result.log` are deliberately two different
files rather than one:

- `results.jsonl` is JSON Lines, written by the **aggregator**, and exists
  so the **display** service can backfill recent results into scrollback
  after a restart (see `services/aggregator/history.py`) — it's an
  internal implementation detail of that replay, not meant to be read
  directly.
- `result.log` is plain text, written by the **display** service, and
  exists for *you* — the exact RESULT block the terminal prints (halves
  table, settled Total ladder, ✓ on the winning side), one per finished
  round, timestamped, at a fixed width regardless of terminal size, meant
  to be opened directly for reviewing rounds — e.g. spotting patterns
  across results for predictions. Only genuinely new finishes are
  appended — the startup backfill replay is deliberately not written here,
  or every restart would duplicate however many rounds it replays (see
  `BACKFILL_COUNT` in `services/display/main.py`).

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
- **No automated tests yet.** Verified so far by running the full stack
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

Phase 1 (this) is extraction + real-time terminal display only — no
betting actions. Later phases build on the same event bus: a betting
service subscribing to `xbet.match_events` (or a new pattern-detection
service sitting between aggregator and it) is the natural next addition,
following the same "just another subscriber" shape described in
[Architecture](#architecture).
