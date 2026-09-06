// Virtual football pattern-betting bot.
//
// Watches BetPawa Cameroon's virtual "English League" (leagueId=7794), runs
// every registered betting pattern (lib/patterns/) against the recent round
// results, and places a single-leg bet through the real UI when one fires.
// Attaches to the team's shared, already-logged-in Chrome over CDP — see
// /home/cdjinguet/CLAUDE.md for that setup. Never launches or closes a
// browser of its own.
//
// This file is the composition root only: it wires the layers together and
// owns the poll loop. The actual work lives in:
//   lib/config.js              CLI flags + env  -> one frozen config object
//   lib/logger.js              file + console logging
//   lib/state-store.js         durable state, per-pattern namespaces, migration
//   lib/audit-log.js           JSONL record of every placement attempt
//   lib/betpawa/api.js         the virtual-sports HTTP API
//   lib/betpawa/rounds.js      pure round/score domain logic
//   lib/betpawa/betting-ui.js  the DOM action layer that actually clicks
//   lib/display.js             the NEXT/RESULT terminal blocks
//   lib/patterns/              the pattern registry — add new patterns here
//   lib/pattern-cycle.js       the per-pattern count/fire/skip life cycle
//   lib/pattern-engine.js      gating and placement orchestration
//
// Usage:
//   node virtual-pattern-bot.js
//   node virtual-pattern-bot.js --dry-run
//   node virtual-pattern-bot.js --stake=25
//   node virtual-pattern-bot.js --patterns=low-scoring-streak
//
// See lib/config.js for the full list of env/CLI knobs.

import { chromium } from 'playwright';

import { loadConfig } from './lib/config.js';
import { createLogger } from './lib/logger.js';
import { createStateStore } from './lib/state-store.js';
import { createAuditLog } from './lib/audit-log.js';
import { createPatternEngine } from './lib/pattern-engine.js';
import { getEnabledPatterns, maxWindowSize } from './lib/patterns/index.js';
import { fetchSeasonsActual, fetchRoundEvents } from './lib/betpawa/api.js';
import {
    sortRoundsAsc,
    findNextUpcomingRound,
    getBettingRound,
    getSettledWindow,
    getRowOneFixture,
    isFixtureFinalized,
    isResultOverdue,
    trailingKnownSums,
    getFullTimeScore,
    getScoreDisplay,
    getOverUnderOdds,
} from './lib/betpawa/rounds.js';
import { renderUpcomingOdds, renderResult, renderLegend } from './lib/display.js';
import { ensureActionPage, placeBet, VIRTUAL_SPORTS_URL } from './lib/betpawa/betting-ui.js';

// The pattern that owned the top-level cooldown/betRoundIds fields before
// state was namespaced per pattern; see migrateState.
const LEGACY_PATTERN_ID = 'high-scoring-pair';

// Rounds of history to resolve BEYOND what the widest pattern needs. A result
// can land long after its round has stopped being the newest one (see
// RESULT_GRACE_MS); without this slack nothing would ever fetch it — the round
// has already dropped out of the pattern window by the time it posts — and it
// would stay a hole in the history forever, blocking every future window that
// spans it. Cached rounds cost no request, so reaching further back is close
// to free.
const LATE_RESULT_SLACK = 6;

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

const withRetry = (log) => async (fn, retries = 3, delay = 1000) => {
    for (let i = 0; i < retries; i++) {
        try {
            return await fn();
        } catch (e) {
            if (i === retries - 1) throw e;
            log(`Attempt ${i + 1} failed (${e.message}), retrying in ${delay}ms...`);
            await sleep(delay);
        }
    }
};

async function main() {
    const config = loadConfig();
    const { log } = createLogger({ dir: config.logDir });
    const retry = withRetry(log);

    const patterns = getEnabledPatterns(config);
    if (!patterns.length) throw new Error('no patterns enabled — check VIRTUAL_PATTERNS / --patterns');
    const historySize = maxWindowSize(patterns);

    const store = createStateStore({ filePath: config.statePath, legacyPatternId: LEGACY_PATTERN_ID });
    const auditLog = createAuditLog(config.auditLogPath);

    log(`Starting virtual-pattern-bot (dryRun=${config.dryRun}, maxBetsPerRun=${config.maxBetsPerRun}, pollInterval=${config.pollIntervalMs}ms, history=${historySize} rounds)`);
    for (const p of patterns) {
        const s = config.forPattern(p.id);
        log(`  pattern "${p.id}": ${p.name} | stake=${s.stakeFcfa} FCFA, cooldown=${s.cooldownRounds} rounds`);
    }
    for (const p of patterns) {
        const { counted, skipRemaining } = store.forPattern(p.id).cycle;
        if (skipRemaining > 0) log(`  [${p.id}] carried over from a previous run — ${skipRemaining} round(s) still to skip before counting restarts`);
        else if (counted > 0) log(`  [${p.id}] carried over from a previous run — ${counted}/${p.windowSize} of the way through a counting block`);
    }
    for (const line of renderLegend()) log(line);
    store.save(); // persist the migrated shape immediately, before any betting decision

    let browser = await chromium.connectOverCDP(config.cdpEndpoint);
    let context = browser.contexts()[0];
    // fetch() from about:blank has no origin and is blocked cross-origin —
    // the page must actually be on betpawa.cm before in-page fetch() to its
    // API will work.
    let apiPage = await context.newPage();
    await apiPage.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });
    let actionPage = null;

    const printedResults = new Set();      // display dedupe: print each result once, not every poll
    const unresolvableRounds = new Set();  // rounds whose result never posted — don't re-fetch them forever
    let waitingOnRoundId = null;           // the round the "waiting on results" line was last logged for

    const engine = createPatternEngine({
        patterns,
        config,
        store,
        auditLog,
        log,
        placeBet: async (bet) => {
            actionPage = await ensureActionPage(context, actionPage);
            return placeBet(actionPage, { ...bet, dryRun: config.dryRun, log });
        },
    });

    let shuttingDown = false;
    const shutdown = async () => {
        if (shuttingDown) return;
        shuttingDown = true;
        log('Shutting down...');
        store.save();
        // Close only the tabs this process itself opened, so repeated runs
        // don't accumulate stray tabs in the team's shared Chrome window.
        // Never call browser.close() — that would kill the browser for
        // everyone else connected to it.
        await apiPage.close().catch(() => {});
        if (actionPage) await actionPage.close().catch(() => {});
        process.exit(0);
    };
    process.on('SIGINT', () => void shutdown());
    process.on('SIGTERM', () => void shutdown());

    async function reconnectCdp() {
        log('Reconnecting to CDP...');
        browser = await chromium.connectOverCDP(config.cdpEndpoint);
        context = browser.contexts()[0];
        apiPage = await context.newPage();
        await apiPage.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });
        actionPage = null;
        log('CDP reconnected');
    }

    async function pollOnce() {
        const seasons = await retry(() => fetchSeasonsActual(apiPage));
        if (!seasons.length) return; // transient, worth a silent retry, not a log line every 15s

        // `seasons/list/actual` returns several sequential seasons at once
        // (the just-finishing one, the current one, and the next one queued
        // up) — items[0] is NOT reliably "the current season" (it can be one
        // that already ended). Flatten every season's rounds into one
        // globally time-sorted list and pick "next" from that combined list;
        // this also handles season rollover for free, with no special-casing
        // needed right at a season boundary.
        const roundsAsc = sortRoundsAsc(seasons.flatMap((s) => s.rounds.map((r) => ({ ...r, seasonId: s.id }))));
        const nextInfo = findNextUpcomingRound(roundsAsc);
        if (!nextInfo) return;

        const bettingRound = getBettingRound(roundsAsc, nextInfo.index);
        if (!bettingRound) return; // nothing open for betting yet (e.g. very start of a fresh round list)

        if (store.seasonId !== bettingRound.seasonId) log(`season: ${store.seasonId ?? '(none)'} -> ${bettingRound.seasonId}`);
        store.seasonId = bettingRound.seasonId;

        // The betting round's row-1 fixture, fetched at most once per poll and
        // shared by the odds capture below and by any pattern that fires.
        let bettingFixture;
        const resolveFixture = async () => {
            if (bettingFixture === undefined) {
                const events = await retry(() => fetchRoundEvents(apiPage, bettingRound.id));
                bettingFixture = getRowOneFixture(events);
            }
            return bettingFixture;
        };

        // Capture this round's O/U odds while they still exist. The site drops
        // a round's markets the moment it kicks off (see getOverUnderOdds), so
        // this is the only window in which the "before" half of the pairing can
        // be obtained — miss it and the result prints with no odds beside it,
        // forever. Guarded by the store, so it costs one fetch per ROUND, not
        // one per poll, and survives a restart mid-round.
        let capturedOdds = null;
        if (!store.getRoundOdds(bettingRound.id)) {
            const fixture = await resolveFixture();
            const lines = fixture ? getOverUnderOdds(fixture) : [];
            if (lines.length) {
                store.recordRoundOdds(bettingRound.id, lines, bettingRound.tradingTime.start);
                store.save();
                capturedOdds = { round: bettingRound, fixture, lines, countdownRound: nextInfo.round };
            }
        }

        // Printed AFTER the result block below, so each poll reads
        // chronologically: how the last round finished, then what is on offer
        // for the next one. Deferred rather than printed inline because every
        // early return below still has to emit it.
        const printCapturedOdds = () => {
            if (!capturedOdds) return;
            for (const line of renderUpcomingOdds(capturedOdds)) log(line);
            capturedOdds = null;
        };

        // Expected/normal on most polls (the newest round is simply still
        // running) — not worth a log line every 15s, so this stays silent.
        // Deliberately reaches further back than any pattern needs: see
        // LATE_RESULT_SLACK.
        const window = getSettledWindow(roundsAsc, nextInfo.index, historySize + LATE_RESULT_SLACK);
        if (!window.length) {
            printCapturedOdds();
            return;
        }

        const newestRound = window[window.length - 1];

        // Resolve each round's goal total, oldest -> newest, reusing the
        // cached value where we already have it — a settled round therefore
        // costs one fetch ever, not one per poll.
        //
        // A round that will not resolve does NOT stop the scan. Two very
        // different things look identical at a single point in time: a result
        // that is merely late, and one that will never come (confirmed live —
        // the four rounds either side of a season rollover published no result
        // for any of their fixtures, and still had none an hour later). The
        // old code abandoned the whole poll at the first of either, which is
        // what let one unresolvable round silence the bot completely: no
        // results printed, no patterns evaluated, and so no bets placed, for
        // as long as that round sat in the window.
        const resolved = [];
        let recorded = false;
        for (const round of window) {
            const isNewest = round.id === newestRound.id;
            const cached = store.getRoundSum(round.id);
            // The newest round is re-fetched once per process when its sum was
            // already cached by an earlier run: the cache holds sums, not
            // scorelines, so a restart would otherwise print no RESULT block
            // at all until the next round landed.
            if (cached !== null && !(isNewest && !printedResults.has(round.id))) {
                resolved.push(cached);
                continue;
            }
            if (unresolvableRounds.has(round.id)) {
                resolved.push(null);
                continue;
            }

            const events = await retry(() => fetchRoundEvents(apiPage, round.id));
            const fixture = getRowOneFixture(events);
            if (!fixture || !isFixtureFinalized(fixture)) {
                if (isResultOverdue(round)) {
                    unresolvableRounds.add(round.id);
                    log(`MD ${round.name} (round ${round.id}) never published a result — treating it as a permanent gap in the history`);
                    resolved.push(null);
                } else {
                    resolved.push(undefined); // still settling; try again next poll
                }
                continue;
            }

            const { sum } = getFullTimeScore(fixture);
            store.recordRoundSum(round.id, sum, round.tradingTime.start);
            recorded = true;
            resolved.push(sum);

            // Printed the moment a round resolves rather than only while it is
            // still the newest one, so a result that arrives a few rounds late
            // gets its RESULT block — in the order the rounds were played —
            // instead of being swallowed for being overtaken.
            if (!printedResults.has(round.id)) {
                printedResults.add(round.id);
                const score = getScoreDisplay(fixture);
                const lines = store.getRoundOdds(round.id);
                for (const line of renderResult({ round, fixture, score, lines })) log(line);
            }
        }
        if (recorded) store.save();

        printCapturedOdds();

        // Patterns judge the rounds IMMEDIATELY before the one they would bet
        // on, so only an unbroken run of results ending at the newest settled
        // round can be acted on. When the newest one has not landed yet that
        // run is empty and nothing fires: quietly evaluating a window that is
        // a few rounds stale would be a different strategy from the one each
        // pattern actually describes.
        const sums = trailingKnownSums(resolved);
        if (!sums.length) {
            // Once per round, not once per poll — this is the normal shape of
            // a settlement backlog, and it has to be visible rather than
            // looking like the bot has simply stopped.
            if (waitingOnRoundId !== newestRound.id) {
                waitingOnRoundId = newestRound.id;
                const lateMin = Math.round((Date.now() - Date.parse(newestRound.tradingTime.end)) / 60000);
                log(`waiting on results: MD ${newestRound.name} (round ${newestRound.id}) closed ${lateMin}m ago and has not settled — no pattern can be judged for MD ${bettingRound.name} until it does`);
            }
            return;
        }
        waitingOnRoundId = null;

        // `sums` is the TRAILING run of `window`, so the round ids that go with
        // it are that same tail — the engine needs them to tell which rounds a
        // pattern's cycle has already consumed.
        const roundIds = window.slice(window.length - sums.length).map((r) => r.id);

        await engine.run({ sums, roundIds, bettingRound, resolveFixture });
        store.save();
    }

    while (!shuttingDown) {
        try {
            await pollOnce();
        } catch (err) {
            if (/closed|disconnected/i.test(err.message || '')) {
                log(`CDP session lost (${err.message}), reconnecting...`);
                try {
                    await reconnectCdp();
                } catch (reErr) {
                    log(`reconnect failed: ${reErr.message}`);
                }
            } else {
                log(`poll error: ${err.message}`);
            }
        }
        await sleep(config.pollIntervalMs);
    }
}

if (import.meta.url === `file://${process.argv[1]}`) {
    main().catch((err) => {
        console.error(err);
        process.exit(1);
    });
}
