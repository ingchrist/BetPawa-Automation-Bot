// Read-only pattern inspector: prints the recent settled rounds with their
// goal totals and reports, for every registered pattern, whether its window
// currently MATCHES.
//
// A match is not a fire. The bot judges non-overlapping blocks on a per-pattern
// counter (lib/pattern-cycle.js), so a pattern whose last N rounds all qualify
// still will not bet unless its counter happens to be at N right now. This
// script deliberately does not read that counter — it answers "does the recent
// history look like this pattern", not "will the bot bet on the next round".
//
// Touches no betslip, clicks nothing, writes no state — safe to run at any
// time, including alongside a live bot.
//
// Usage: node scripts/check-patterns.js

import { chromium } from 'playwright';
import { loadConfig } from '../lib/config.js';
import { ALL_PATTERNS, maxWindowSize } from '../lib/patterns/index.js';
import { fetchSeasonsActual, fetchRoundEvents } from '../lib/betpawa/api.js';
import {
    sortRoundsAsc,
    findNextUpcomingRound,
    getBettingRound,
    getSettledWindow,
    getRowOneFixture,
    isFixtureFinalized,
    getScoreDisplay,
    formatFixtureLine,
    formatCountdown,
} from '../lib/betpawa/rounds.js';
import { VIRTUAL_SPORTS_URL } from '../lib/betpawa/betting-ui.js';

async function main() {
    const config = loadConfig();
    const browser = await chromium.connectOverCDP(config.cdpEndpoint);
    const context = browser.contexts()[0];
    const page = await context.newPage();

    try {
        await page.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });

        const seasons = await fetchSeasonsActual(page);
        const roundsAsc = sortRoundsAsc(seasons.flatMap((s) => s.rounds.map((r) => ({ ...r, seasonId: s.id }))));
        const nextInfo = findNextUpcomingRound(roundsAsc);
        if (!nextInfo) throw new Error('no upcoming round found');
        const bettingRound = getBettingRound(roundsAsc, nextInfo.index);

        const window = getSettledWindow(roundsAsc, nextInfo.index, maxWindowSize(ALL_PATTERNS));
        console.log(`betting round: ${bettingRound?.id} (season ${bettingRound?.seasonId}) | next round starts in ${formatCountdown(nextInfo.round)}\n`);
        console.log(`settled window (oldest -> newest), ${window.length} round(s):`);

        const sums = [];
        for (const round of window) {
            const fixture = getRowOneFixture(await fetchRoundEvents(page, round.id));
            if (!fixture || !isFixtureFinalized(fixture)) {
                console.log(`  ${round.id}  (not finalized yet)`);
                sums.push(null);
                continue;
            }
            const score = getScoreDisplay(fixture);
            sums.push(score.sum);
            console.log(`  ${round.id}  ${formatFixtureLine(fixture, score)}   sum=${score.sum}`);
        }

        console.log('\npattern windows (a MATCH is not a bet — see the note at the top of this file):');
        for (const pattern of ALL_PATTERNS) {
            const slice = sums.slice(-pattern.windowSize);
            const usable = slice.length === pattern.windowSize && slice.every((s) => s !== null);
            if (!usable) {
                console.log(`  ${pattern.id.padEnd(20)} not enough settled history (needs ${pattern.windowSize}, has ${slice.filter((s) => s !== null).length})`);
                continue;
            }
            const matches = pattern.evaluate(slice);
            console.log(`  ${pattern.id.padEnd(20)} ${matches ? 'MATCH' : 'no   '}  sums=[${slice.join(', ')}] -> ${pattern.bet.marketTab} / ${pattern.bet.selectionLabel}`);
        }
    } finally {
        await page.close().catch(() => {});
        process.exit(process.exitCode || 0);
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
