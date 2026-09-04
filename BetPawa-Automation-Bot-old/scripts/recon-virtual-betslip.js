// Re-run this if betpawa.cm's markup changes and virtual-pattern-betting.js
// starts failing to find its selectors. Attaches to the team's shared,
// already-logged-in Chrome over CDP (see /home/cdjinguet/CLAUDE.md) and
// prints the current selectors/data-test-ids for the virtual-sports
// upcoming odds board: the O/U tab, a fixture card, its "Under 3.5" button,
// the stake input, the Place Bet button, and the balance display.
//
// Clicks "Under 3.5" (to make the stake input/Place Bet button render) and
// then clears the betslip again — never clicks "Place Bet" itself, so no
// real bet is ever placed by this script.
//
// Usage: node scripts/recon-virtual-betslip.js

import { chromium } from 'playwright';

const CDP_ENDPOINT = process.env.CDP_ENDPOINT || 'http://127.0.0.1:9222';
const VIRTUAL_SPORTS_URL = 'https://www.betpawa.cm/virtual-sports?virtualTab=upcoming&leagueId=7794';

async function main() {
    const browser = await chromium.connectOverCDP(CDP_ENDPOINT);
    const context = browser.contexts()[0];
    const page = await context.newPage();

    try {
        await page.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });
        // Wait for the fixture list itself, not just any [role="tab"] — the
        // header's EN/FR language tabs mount before the market tabs (O/U,
        // 1X2, ...), which only appear once the odds board's fetch resolves.
        await page.waitForSelector('.VirtualUpcomingTab_betContainer__QzW5H', { timeout: 15000 });

        // Activate O/U if not already active (site remembers the last active
        // tab across round refreshes, so this may be a no-op).
        const ouActive = await page.evaluate(() => {
            const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
            const ou = tabs.find((t) => t.textContent.trim() === 'O/U');
            return ou ? ou.getAttribute('aria-selected') === 'true' : false;
        });
        if (!ouActive) {
            const box = await page.evaluate(() => {
                const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
                const ou = tabs.find((t) => t.textContent.trim() === 'O/U');
                if (!ou) return null;
                const r = ou.getBoundingClientRect();
                return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
            });
            if (!box) throw new Error('O/U tab not found — market tabs may not have rendered yet');
            await page.mouse.click(box.x, box.y);
            await page.waitForTimeout(500);
        }

        const before = await page.evaluate(() => {
            const cardSel = '.VirtualUpcomingTab_betContainer__QzW5H';
            const cards = Array.from(document.querySelectorAll(cardSel));
            const card0 = cards[0];
            const title = card0 ? card0.querySelector('.VirtualUpcomingTab_betTitle__Oofi6') : null;
            const under35Btn = card0
                ? Array.from(card0.querySelectorAll('button[data-test-id^="odd-"]')).find((b) => {
                      const lbl = b.querySelector('._label_r1b54_47');
                      return lbl && lbl.textContent.trim() === 'Under 3.5';
                  })
                : null;
            const balanceEl = document.querySelector('.CurrencySelector_balanceMeasure__Y1cb3');
            if (!under35Btn) return { fixtureCardCount: cards.length, rowOneTeam: title ? title.textContent.trim() : null, under35Found: false };
            const r = under35Btn.getBoundingClientRect();
            return {
                fixtureCardCount: cards.length,
                rowOneTeam: title ? title.textContent.trim() : null,
                under35TestId: under35Btn.getAttribute('data-test-id'),
                under35Found: true,
                under35Box: { x: r.x + r.width / 2, y: r.y + r.height / 2 },
                balanceText: balanceEl ? balanceEl.textContent.trim() : null,
            };
        });

        // Click Under 3.5 to make the stake input / Place Bet button render
        // (they only exist once the betslip has a selection). This adds a
        // selection but commits no money.
        if (before.under35Found) {
            await page.mouse.click(before.under35Box.x, before.under35Box.y);
            await page.waitForSelector('input[data-test-id="stake-input"]', { timeout: 10000 }).catch(() => {});
        }

        const recon = await page.evaluate((before) => {
            const stakeInput = document.querySelector('input[data-test-id="stake-input"]');
            const placeBetBtn = document.querySelector('button[data-test-id="place-bet-button"]');
            return {
                fixtureCardCount: before.fixtureCardCount,
                rowOneTeam: before.rowOneTeam,
                under35TestId: before.under35TestId,
                under35Found: before.under35Found,
                stakeInputTestId: stakeInput ? stakeInput.getAttribute('data-test-id') : null,
                placeBetButtonTestId: placeBetBtn ? placeBetBtn.getAttribute('data-test-id') : null,
                placeBetButtonText: placeBetBtn ? placeBetBtn.textContent.trim() : null,
                balanceText: before.balanceText,
            };
        }, before);

        console.log(JSON.stringify(recon, null, 2));

        // Clear the betslip again so this script leaves no pending selection
        // behind (does not click "Place Bet" — no money ever committed).
        const clearBox = await page.evaluate(() => {
            const el = Array.from(document.querySelectorAll('*')).find(
                (e) => e.children.length === 0 && e.textContent.trim() === 'Clear Betslip'
            );
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        });
        if (clearBox) await page.mouse.click(clearBox.x, clearBox.y);

        if (!recon.under35Found || !recon.stakeInputTestId || !recon.placeBetButtonTestId) {
            console.error('\nWARNING: one or more expected selectors were not found — the site markup may have changed. Update SEL in lib/virtual-pattern-betting.js.');
            process.exitCode = 1;
        }
    } finally {
        await page.close();
        // Never call browser.close() here — this is a connectOverCDP session
        // attached to the team's shared Chrome; closing it would kill the
        // browser for everyone else. Its transport otherwise keeps the event
        // loop alive, so exit explicitly instead of letting Node drain.
        process.exit(process.exitCode || 0);
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
