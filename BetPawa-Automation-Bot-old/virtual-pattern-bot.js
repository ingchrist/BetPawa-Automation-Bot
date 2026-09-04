// Virtual football pattern-betting bot.
//
// Watches BetPawa Cameroon's virtual "English League" (leagueId=7794) for a
// streak pattern (see lib/virtual-pattern-detector.js) and places a flat
// 5 FCFA "Under 3.5" bet through the real UI (lib/virtual-pattern-betting.js)
// when it fires. Attaches to the team's shared, already-logged-in Chrome
// over CDP — see /home/cdjinguet/CLAUDE.md for that setup. Never launches or
// closes a browser of its own.
//
// Usage:
//   node virtual-pattern-bot.js              # live, places real bets
//   node virtual-pattern-bot.js --dry-run     # detection + logging only
//
// Env (see .env.example): CDP_ENDPOINT, VIRTUAL_MAX_BETS_PER_RUN,
// VIRTUAL_STAKE_FCFA, VIRTUAL_POLL_INTERVAL_MS

import { chromium } from 'playwright';
import * as dotenv from 'dotenv';
import fs from 'fs';
import path from 'path';

import {
    fetchSeasonsActual,
    fetchRoundEvents,
    sortRoundsAsc,
    findNextUpcomingRound,
    getRowOneFixture,
    isFixtureFinalized,
    isRoundSettled,
    getFullTimeScore,
    getScoreDisplay,
    formatFixtureLine,
    formatCountdown,
    evaluatePattern,
} from './lib/virtual-pattern-detector.js';
import { ensureActionPage, placeUnder35Bet, VIRTUAL_SPORTS_URL } from './lib/virtual-pattern-betting.js';

const env = dotenv.config().parsed || {};

const CDP_ENDPOINT = process.env.CDP_ENDPOINT || env.CDP_ENDPOINT || 'http://127.0.0.1:9222';
const MAX_BETS_PER_RUN = Number(process.env.VIRTUAL_MAX_BETS_PER_RUN || env.VIRTUAL_MAX_BETS_PER_RUN || 5);
const STAKE_FCFA = Number(process.env.VIRTUAL_STAKE_FCFA || env.VIRTUAL_STAKE_FCFA || 5);
const POLL_INTERVAL_MS = Number(process.env.VIRTUAL_POLL_INTERVAL_MS || env.VIRTUAL_POLL_INTERVAL_MS || 15000);
const DRY_RUN = process.argv.includes('--dry-run');

const STATE_PATH = path.join('storage', 'virtual-pattern-state.json');
const AUDIT_LOG_PATH = path.join('storage', 'logs', 'virtual-pattern-bets.jsonl');

// --- logging (same convention as betpawa-bot.js) ---------------------------

const setupLogging = () => {
    const logDir = 'logs';
    if (!fs.existsSync(logDir)) fs.mkdirSync(logDir);

    const today = new Date().toISOString().split('T')[0];
    const logFile = fs.createWriteStream(path.join(logDir, `virtual-pattern-bot-${today}.log`), { flags: 'a' });

    const cleanupOldLogs = () => {
        const sevenDaysAgo = new Date();
        sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
        for (const file of fs.readdirSync(logDir)) {
            const filePath = path.join(logDir, file);
            if (fs.statSync(filePath).mtime < sevenDaysAgo) fs.unlinkSync(filePath);
        }
    };
    cleanupOldLogs();

    return {
        log: (message) => {
            const line = `[${new Date().toISOString()}] ${message}\n`;
            logFile.write(line);
            console.log(message);
        },
    };
};

const { log } = setupLogging();

const retry = async (fn, retries = 3, delay = 1000) => {
    for (let i = 0; i < retries; i++) {
        try {
            return await fn();
        } catch (e) {
            if (i === retries - 1) throw e;
            log(`Attempt ${i + 1} failed (${e.message}), retrying in ${delay}ms...`);
            await new Promise((res) => setTimeout(res, delay));
        }
    }
};

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

// --- state persistence ------------------------------------------------------

function defaultState() {
    return {
        seasonId: null,
        betRoundIds: [],
        lastPatternSums: {},
        lastEvaluatedNextRoundId: null,
        updatedAt: null,
    };
}

function loadState() {
    try {
        const raw = fs.readFileSync(STATE_PATH, 'utf8');
        return { ...defaultState(), ...JSON.parse(raw) };
    } catch {
        return defaultState();
    }
}

function saveState(state) {
    state.updatedAt = new Date().toISOString();
    const dir = path.dirname(STATE_PATH);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const tmpPath = `${STATE_PATH}.tmp`;
    fs.writeFileSync(tmpPath, JSON.stringify(state, null, 2));
    fs.renameSync(tmpPath, STATE_PATH);
}

// Keep only the most recent N rounds' sums so the state file doesn't grow
// forever; these are observability aids only, never authoritative.
function pruneOldSums(state, keep = 20) {
    const ids = Object.keys(state.lastPatternSums).sort((a, b) => Number(a) - Number(b));
    for (const id of ids.slice(0, Math.max(0, ids.length - keep))) {
        delete state.lastPatternSums[id];
    }
}

function appendJsonl(filePath, obj) {
    const dir = path.dirname(filePath);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(filePath, JSON.stringify(obj) + '\n');
}

// --- main --------------------------------------------------------------------

async function main() {
    log(`Starting virtual-pattern-bot (dryRun=${DRY_RUN}, maxBetsPerRun=${MAX_BETS_PER_RUN}, stake=${STAKE_FCFA} FCFA, pollInterval=${POLL_INTERVAL_MS}ms)`);

    const state = loadState();
    let betsPlacedThisRun = 0; // process-local only, per the user's "per-run" cap — intentionally not persisted
    let lastLoggedResultRoundId = null; // display dedupe: print each new result once, not every poll
    let lastFireAnnouncedForRound = null; // display dedupe: announce a FIRE once per target round
    let browser = await chromium.connectOverCDP(CDP_ENDPOINT);
    let context = browser.contexts()[0];
    let apiPage = await context.newPage();
    // fetch() from about:blank has no origin and is blocked cross-origin —
    // the page must actually be on betpawa.cm before in-page fetch() to its
    // API will work.
    await apiPage.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });
    let actionPage = null;

    let shuttingDown = false;
    const shutdown = async () => {
        if (shuttingDown) return;
        shuttingDown = true;
        log('Shutting down...');
        saveState(state);
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
        browser = await chromium.connectOverCDP(CDP_ENDPOINT);
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
        const allRounds = seasons.flatMap((s) => s.rounds.map((r) => ({ ...r, seasonId: s.id })));
        const roundsAsc = sortRoundsAsc(allRounds);
        const nextInfo = findNextUpcomingRound(roundsAsc);
        if (!nextInfo) return;

        const { round: nextRound, index } = nextInfo;

        // roundsAsc[index] is the first round whose OWN trading window hasn't
        // started yet — but the real site is NOT open for new bets on that
        // round. Confirmed live (matched the site's on-screen row-1 fixture
        // against exact round timestamps): the site is still showing/taking
        // bets on roundsAsc[index-1] — the round that started but hasn't
        // ended — right up until its tradingTime.end (which equals
        // roundsAsc[index].tradingTime.start, so the "Starts in" countdown
        // the site displays next to that fixture is really counting down to
        // THIS round's own close, not to when betting opens). So the round
        // to actually act on — fetch row-1 for, and place the bet on — is
        // index-1, not index.
        const bettingRound = roundsAsc[index - 1];
        if (!bettingRound) return; // nothing open for betting yet (e.g. very start of a fresh round list)

        if (state.seasonId !== bettingRound.seasonId) log(`season: ${state.seasonId ?? '(none)'} -> ${bettingRound.seasonId}`);
        state.seasonId = bettingRound.seasonId;

        // The two truly-completed rounds for the pattern are the two rounds
        // immediately before bettingRound (index-1) — confirmed live that a
        // round's result only posts within seconds of its own window
        // closing, i.e. right as it would stop being "index-1" and roll into
        // "index-2", so it can never be observed as finalized while still
        // occupying index-1.
        const prev1 = roundsAsc[index - 2];
        const prev2 = roundsAsc[index - 3];
        if (!prev1 || !prev2) return;

        // Confirmed live: a round's full-time score can appear with a
        // provisional value that gets corrected a few seconds later, so
        // presence of the field alone isn't proof of finality — also
        // require enough real time to have passed since the round's window
        // closed (see isRoundSettled's comment for the measured basis).
        // Expected/normal on most polls (the round is simply still running)
        // — not worth a log line every 15s, so this stays silent.
        if (!isRoundSettled(prev1) || !isRoundSettled(prev2)) return;

        const [ev1, ev2] = await Promise.all([
            retry(() => fetchRoundEvents(apiPage, prev1.id)),
            retry(() => fetchRoundEvents(apiPage, prev2.id)),
        ]);
        const fx1 = getRowOneFixture(ev1);
        const fx2 = getRowOneFixture(ev2);
        if (!fx1 || !fx2 || !isFixtureFinalized(fx1) || !isFixtureFinalized(fx2)) return; // transient, self-corrects

        const s1 = getFullTimeScore(fx1).sum;
        const s2 = getFullTimeScore(fx2).sum;
        state.lastPatternSums[prev1.id] = s1;
        state.lastPatternSums[prev2.id] = s2;
        pruneOldSums(state);
        state.lastEvaluatedNextRoundId = bettingRound.id;

        // prev1/prev2 stay constant for an entire ~5min round window, so
        // without this guard the same line (and any FIRE/cap/already-bet
        // announcement below) would repeat on every 15s poll. Show each
        // newly-completed result exactly once, one fixture at a time —
        // mirroring the site's own live feed — with the countdown to the
        // round it would bet on if the pattern has now fired.
        const isNewResult = prev1.id !== lastLoggedResultRoundId;
        if (isNewResult) {
            lastLoggedResultRoundId = prev1.id;
            const score1 = getScoreDisplay(fx1);
            log(`${formatFixtureLine(fx1, score1)}   sum=${score1.sum}   |  Next round starts in: ${formatCountdown(nextRound)}`);
        }

        const fires = evaluatePattern({ sum1: s1, sum2: s2 });
        if (!fires) return saveState(state);

        const alreadyAnnouncedThisFire = lastFireAnnouncedForRound === bettingRound.id;
        if (isNewResult || !alreadyAnnouncedThisFire) {
            lastFireAnnouncedForRound = bettingRound.id;
            log(`PATTERN FIRE: two consecutive rounds >=4 (sum=${s2}, sum=${s1}) -> betting on next round`);
        }

        if (state.betRoundIds.includes(bettingRound.id)) {
            return saveState(state); // already handled (bet placed or failed) — stay quiet on repeat polls
        }

        if (betsPlacedThisRun >= MAX_BETS_PER_RUN) {
            // Deliberately not marked in betRoundIds: never attempted, so it
            // stays eligible if the operator raises the cap or restarts.
            if (!alreadyAnnouncedThisFire) log(`cap reached (${MAX_BETS_PER_RUN}/run) — skipping bet placement`);
            return saveState(state);
        }

        const evNext = await retry(() => fetchRoundEvents(apiPage, bettingRound.id));
        const fxNext = getRowOneFixture(evNext);
        if (!fxNext) return log(`no row-1 English League fixture yet for round ${bettingRound.id}, will retry next poll`);

        // Mark attempted BEFORE clicking, so a crash mid-click can never
        // result in a retry that double-bets the same round.
        state.betRoundIds.push(bettingRound.id);
        saveState(state);

        try {
            actionPage = await ensureActionPage(context, actionPage);
            const result = await placeUnder35Bet(actionPage, {
                fixtureName: fxNext.name,
                stakeFcfa: STAKE_FCFA,
                dryRun: DRY_RUN,
                log,
            });
            betsPlacedThisRun++;
            appendJsonl(AUDIT_LOG_PATH, {
                timestamp: new Date().toISOString(),
                roundId: bettingRound.id,
                fixture: fxNext.name,
                market: 'Over/Under Full Time',
                selection: 'Under 3.5',
                stake: STAKE_FCFA,
                success: true,
                ...result,
            });
            log(`BET PLACED: round ${bettingRound.id} ${fxNext.name} stake=${STAKE_FCFA} FCFA dryRun=${DRY_RUN}`);
        } catch (err) {
            appendJsonl(AUDIT_LOG_PATH, {
                timestamp: new Date().toISOString(),
                roundId: bettingRound.id,
                fixture: fxNext.name,
                market: 'Over/Under Full Time',
                selection: 'Under 3.5',
                stake: STAKE_FCFA,
                success: false,
                error: err.message,
            });
            log(`BET FAILED: round ${bettingRound.id}: ${err.message}`);
        }
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
        await sleep(POLL_INTERVAL_MS);
    }
}

if (import.meta.url === `file://${process.argv[1]}`) {
    main().catch((err) => {
        console.error(err);
        process.exit(1);
    });
}
