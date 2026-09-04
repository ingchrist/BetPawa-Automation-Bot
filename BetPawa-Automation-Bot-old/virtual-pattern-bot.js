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
    formatFixtureLine,
    formatCountdown,
} from './lib/betpawa/rounds.js';
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

        // Expected/normal on most polls (the newest round is simply still
        // running) — not worth a log line every 15s, so this stays silent.
        const window = getSettledWindow(roundsAsc, nextInfo.index, historySize);
        if (!window.length) return;

        const newestRound = window[window.length - 1];
        const needsDisplay = newestRound.id !== lastLoggedResultRoundId;

        // Resolve each round's goal total, reusing the cached value where we
        // already have it — a 5-round window therefore costs one fetch per new
        // round, not five per poll. The newest round is (re)fetched when it
        // still has to be printed, since the cache holds sums, not scorelines.
        const sums = [];
        let newestFixture = null;
        for (const round of window) {
            const cached = store.getRoundSum(round.id);
            const isNewest = round.id === newestRound.id;
            if (cached !== null && !(isNewest && needsDisplay)) {
                sums.push(cached);
                continue;
            }
            const events = await retry(() => fetchRoundEvents(apiPage, round.id));
            const fixture = getRowOneFixture(events);
            if (!fixture || !isFixtureFinalized(fixture)) return; // transient, self-corrects on the next poll
            const { sum } = getFullTimeScore(fixture);
            store.recordRoundSum(round.id, sum, round.tradingTime.start);
            sums.push(sum);
            if (isNewest) newestFixture = fixture;
        }

        // Show each newly-completed result exactly once, one fixture at a
        // time — mirroring the site's own live feed — with the countdown to
        // the round a bet would go on.
        if (needsDisplay && newestFixture) {
            lastLoggedResultRoundId = newestRound.id;
            const score = getScoreDisplay(newestFixture);
            log(`${formatFixtureLine(newestFixture, score)}   sum=${score.sum}   |  Next round starts in: ${formatCountdown(nextInfo.round)}`);
        }

        // The betting round's own fixture is only needed if something
        // actually fires, so it is fetched lazily and at most once per poll.
        let bettingFixture;
        const resolveFixture = async () => {
            if (bettingFixture === undefined) {
                const events = await retry(() => fetchRoundEvents(apiPage, bettingRound.id));
                bettingFixture = getRowOneFixture(events);
            }
            return bettingFixture;
        };

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
