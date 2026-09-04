# Virtual pattern-betting bot

Watches BetPawa Cameroon's virtual **English League** (`leagueId=7794`), runs every
registered pattern against the recent round results, and places a single-leg bet
through the real UI when one fires.

It attaches to the team's shared, already-logged-in Chrome over CDP (see
`/home/cdjinguet/CLAUDE.md`). It never launches or closes a browser of its own,
and only ever closes the tabs it opened itself.

```bash
npm run virtual-bet              # live — places real bets
npm run virtual-bet:dry          # every step except the final Place Bet click
npm run virtual-bet:check        # read-only: what the patterns see right now
npm run virtual-bet:recon        # live selector check against the real page
```

## Registered patterns

| id | trigger | bet on the next round |
| --- | --- | --- |
| `high-scoring-pair` | 2 consecutive rounds with total goals **>= 4** | `O/U` → **Under 3.5** |
| `low-scoring-streak` | 5 consecutive rounds with total goals **<= 2** | `O/U` → **Over 2.5** |

"Total goals" is the full-time sum of the league's **row-1** fixture — the first
fixture as actually displayed on the site, which is the alphabetically-first one,
not the first in API order.

Each pattern has its **own** cooldown and its own record of rounds it has already
acted on, so they never interfere with each other. At most **one** bet is placed
per round: if two patterns ever fired together, the one listed first in
`lib/patterns/index.js` wins and the other is skipped rather than adding a second
leg (two legs in the betslip is an accumulator, a completely different bet).

## Configuration

Everything is read in one place, `lib/config.js`. CLI flags beat env, env beats
`.env`.

| Env | Default | Meaning |
| --- | --- | --- |
| `CDP_ENDPOINT` | `http://127.0.0.1:9222` | Chrome DevTools endpoint |
| `VIRTUAL_POLL_INTERVAL_MS` | `15000` | how often to poll for results |
| `VIRTUAL_MAX_BETS_PER_RUN` | `5` | safety cap, across all patterns |
| `VIRTUAL_STAKE_FCFA` | `5` | stake per bet — any amount |
| `VIRTUAL_COOLDOWN_ROUNDS` | `3` | rounds to skip after a placement attempt |
| `VIRTUAL_PATTERNS` | all | comma-separated pattern ids to enable |
| `VIRTUAL_LOG_DIR` | `logs` | where the daily log files go |
| `VIRTUAL_STATE_PATH` | `storage/virtual-pattern-state.json` | durable state |
| `VIRTUAL_AUDIT_LOG_PATH` | `storage/logs/virtual-pattern-bets.jsonl` | audit trail |

CLI: `--dry-run`, `--stake=25`, `--patterns=low-scoring-streak`.

Any pattern can override the stake, the cooldown, or be switched off on its own,
using its id upper-snake-cased:

```bash
VIRTUAL_LOW_SCORING_STREAK_STAKE_FCFA=50      # bet 50 FCFA on this pattern only
VIRTUAL_LOW_SCORING_STREAK_COOLDOWN_ROUNDS=6
VIRTUAL_HIGH_SCORING_PAIR_ENABLED=false       # pause just this one
```

## Adding a third pattern

1. Create `lib/patterns/<your-pattern>.js` and default-export a pattern object.
   For the "N consecutive rounds whose total satisfies a predicate" shape, use
   the `createStreakPattern` factory:

   ```js
   import { createStreakPattern } from './streak.js';

   export default createStreakPattern({
       id: 'goalless-run',
       name: '3 consecutive 0-0 rounds -> Over 1.5 on the next round',
       windowSize: 3,
       predicate: (sum) => sum === 0,
       predicateLabel: 'with sum == 0',
       bet: { marketTab: 'O/U', selectionLabel: 'Over 1.5', market: 'Over/Under Full Time', selection: 'Over 1.5' },
   });
   ```

   A pattern that is not a streak just has to expose the same contract
   (`id`, `name`, `windowSize`, `bet`, `evaluate(sums)`, `explain(sums)`) —
   see the typedef at the top of `lib/patterns/streak.js`.

2. Register it in `lib/patterns/index.js`.

That is the whole change. Config keys, state namespacing, cooldowns, the audit
log, history depth and the placement flow are all derived from the pattern's
`id`, `windowSize` and `bet`.

3. `npm run virtual-bet:recon` confirms the new market/selection is reachable on
   the real page, and `npm run virtual-bet:check` shows what it would do right now.

## Architecture

```
virtual-pattern-bot.js        composition root: wiring + the poll loop only
lib/
  config.js                   CLI + env  -> one frozen config object
  logger.js                   file + console logging, 7-day retention
  state-store.js              durable state, per-pattern namespaces, migration
  audit-log.js                JSONL record of every placement attempt
  pattern-engine.js           cooldowns, gating, placement orchestration
  betpawa/
    api.js                    the virtual-sports HTTP API
    rounds.js                 pure round/score domain logic (no I/O)
    betting-ui.js             the DOM action layer that actually clicks
  patterns/
    index.js                  the registry
    streak.js                 factory + the Pattern contract
    high-scoring-pair.js
    low-scoring-streak.js
```

`lib/betpawa/rounds.js` and `lib/patterns/*` are pure and can be tested without a
browser. `lib/betpawa/api.js` treats `page` as an opaque fetch executor — the API
validates a device fingerprint tied to the real browser, so requests must run
inside the logged-in page rather than from a Node HTTP client.

## Timing, and which round is which

The site does **not** take bets on the first round whose trading window has yet to
open. It is still taking bets on the round that has started but not ended, right
up to its `tradingTime.end`. So with `nextIndex` = the first not-yet-started
round:

- `nextIndex - 1` is the **betting round** — the one a bet goes on;
- `nextIndex - 2` is the newest **settled** round — a result only posts within
  seconds of its own window closing;
- the pattern window is the run of settled rounds ending there.

A round's score can appear provisionally and be corrected seconds later, so a
round only counts as settled 60s after its window closed
(`FINALITY_BUFFER_MS`).

Round `id`s are **not** chronological across seasons, so everything sorts and
prunes by `tradingTime.start`, never by id.

## Placement, and how success is detected

`placeBet` navigates, asserts the upcoming-matchday tab (never the in-play "Live"
one), activates the market tab, **clears the betslip**, verifies the row-1 fixture
still matches the one the pattern fired on, clicks the selection, sets the stake,
and submits.

A confirmed placement renders a **"Bet placed!"** panel in the betslip column.
It does *not* return to "Betslip is empty" — an earlier version waited for that
text and logged all nine of its genuinely-successful bets as failures.

Building the betslip is retried; the submit click never is. Retrying an ambiguous
submit risks placing the same real-money bet twice, so it fails closed: the round
is marked as attempted *before* the click, and the pattern goes on cooldown
whether the attempt succeeded or not.
