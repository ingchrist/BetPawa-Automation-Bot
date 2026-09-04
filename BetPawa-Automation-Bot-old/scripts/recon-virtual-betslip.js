// Live validation of lib/betpawa/betting-ui.js against the real site. Run it
// whenever betpawa.cm changes its markup and placements start failing.
//
// It drives the PRODUCTION placeBet() in dry-run mode, so every step the bot
// really performs is exercised — upcoming-matchday tab, market tab, betslip
// clearing, row-1 fixture match, odd-button lookup, stake entry — for every
// registered pattern's market/selection. The only step it never performs is
// the final "Place Bet" click, and it clears the betslip afterwards, so no
// money is ever committed and no selection is left behind.
//
// Usage: node scripts/recon-virtual-betslip.js

import { chromium } from 'playwright';
import { ALL_PATTERNS } from '../lib/patterns/index.js';
import { ensureActionPage, placeBet } from '../lib/betpawa/betting-ui.js';

const CDP_ENDPOINT = process.env.CDP_ENDPOINT || 'http://127.0.0.1:9222';
const ROW_ONE_TITLE = '.VirtualUpcomingTab_betTitle__Oofi6';
const FIXTURE_CARD = '.VirtualUpcomingTab_betContainer__QzW5H';

async function main() {
    const browser = await chromium.connectOverCDP(CDP_ENDPOINT);
    const context = browser.contexts()[0];
    const page = await ensureActionPage(context, null);
    let failures = 0;

    try {
        await page.waitForSelector(FIXTURE_CARD, { timeout: 20000 });
        const fixtureName = await page.evaluate(
            ({ card, title }) => document.querySelector(card)?.querySelector(title)?.textContent.trim() ?? null,
            { card: FIXTURE_CARD, title: ROW_ONE_TITLE }
        );
        if (!fixtureName) throw new Error('could not read the row-1 fixture name');
        console.log(`row-1 fixture: ${fixtureName}\n`);

        for (const pattern of ALL_PATTERNS) {
            const { marketTab, selectionLabel } = pattern.bet;
            try {
                const result = await placeBet(page, {
                    fixtureName,
                    marketTab,
                    selectionLabel,
                    stakeFcfa: 5,
                    dryRun: true,
                    log: (m) => console.log(`  ${m}`),
                });
                console.log(`OK   ${pattern.id}: ${marketTab} / ${selectionLabel} (matchday ${result.matchday})`);
            } catch (err) {
                failures++;
                console.error(`FAIL ${pattern.id}: ${marketTab} / ${selectionLabel} — ${err.message}`);
            }
        }

        if (failures) {
            console.error(`\n${failures} selector/flow check(s) failed — update SEL in lib/betpawa/betting-ui.js.`);
            process.exitCode = 1;
        } else {
            console.log('\nAll patterns can reach the Place Bet step. No bet was placed.');
        }
    } finally {
        await page.close().catch(() => {});
        // Never call browser.close(): this is a connectOverCDP session on the
        // team's shared Chrome, and closing it would kill the browser for
        // everyone else. Its transport keeps the event loop alive, so exit.
        process.exit(process.exitCode || 0);
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
