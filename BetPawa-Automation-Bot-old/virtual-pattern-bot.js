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
//   lib/pattern-engine.js      cooldowns, gating, placement orchestration
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
    getFullTimeScore,
    getScoreDisplay,
    getOverUnderOdds,
} from './lib/betpawa/rounds.js';
import { renderUpcomingOdds, renderResult, renderLegend } from './lib/display.js';
import { ensureActionPage, placeBet, VIRTUAL_SPORTS_URL } from './lib/betpawa/betting-ui.js';

// The pattern that owned the top-level cooldown/betRoundIds fields before
// state was namespaced per pattern; see migrateState.
const LEGACY_PATTERN_ID = 'high-scoring-pair';

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
    const carriedCooldowns = patterns.filter((p) => store.forPattern(p.id).cooldownRoundsRemaining > 0);
    for (const p of carriedCooldowns) {
        log(`  [${p.id}] COOLDOWN carried over from a previous run — ${store.forPattern(p.id).cooldownRoundsRemaining} round(s) still to skip`);
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

    let lastLoggedResultRoundId = null; // display dedupe: print each new result once, not every poll

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
        const window = getSettledWindow(roundsAsc, nextInfo.index, historySize);
        if (!window.length) {
            printCapturedOdds();
            return;
        }

        const newestRound = window[window.length - 1];
        const needsDisplay = newestRound.id !== lastLoggedResultRoundId;

        // Resolve each round's goal total, reusing the cached value where we
        // already have it — a 5-round window therefore costs one fetch per new
        // round, not five per poll. The newest round is (re)fetched when it
        // still has to be printed, since the cache holds sums, not scorelines.
        const sums = [];
        let newestFixture = null;
        let pending = false;
        for (const round of window) {
            const cached = store.getRoundSum(round.id);
            const isNewest = round.id === newestRound.id;
            if (cached !== null && !(isNewest && needsDisplay)) {
                sums.push(cached);
                continue;
            }
            const events = await retry(() => fetchRoundEvents(apiPage, round.id));
            const fixture = getRowOneFixture(events);
            if (!fixture || !isFixtureFinalized(fixture)) {
                pending = true; // transient, self-corrects on the next poll
                break;
            }
            const { sum } = getFullTimeScore(fixture);
            store.recordRoundSum(round.id, sum, round.tradingTime.start);
            sums.push(sum);
            if (isNewest) newestFixture = fixture;
        }
        if (pending) {
            printCapturedOdds();
            return;
        }

        // Show each newly-completed result exactly once, one fixture at a
        // time — mirroring the site's own live feed — alongside the odds this
        // round had been offering, which were captured a poll cycle ago while
        // it was still open.
        if (needsDisplay && newestFixture) {
            lastLoggedResultRoundId = newestRound.id;
            const score = getScoreDisplay(newestFixture);
            const lines = store.getRoundOdds(newestRound.id);
            for (const line of renderResult({ round: newestRound, fixture: newestFixture, score, lines })) log(line);
        }

        printCapturedOdds();

        await engine.run({ sums, settledRoundId: newestRound.id, bettingRound, resolveFixture });
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
