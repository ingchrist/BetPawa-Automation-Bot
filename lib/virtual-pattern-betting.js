// DOM action layer: navigates the real betting UI and places the bet.
// Selectors below were captured live against the real page during
// scripts/recon-virtual-betslip.js (see that file for how to re-derive them
// if betpawa.cm changes its markup).

import fs from 'fs';
import path from 'path';

export const VIRTUAL_SPORTS_URL = 'https://www.betpawa.cm/virtual-sports?virtualTab=upcoming&leagueId=7794';

const SEL = {
    // Repeating fixture-card container; DOM order === on-screen display order,
    // so index 0 is always "row 1" for whichever league filter is active.
    fixtureCard: '.VirtualUpcomingTab_betContainer__QzW5H',
    fixtureTitle: '.VirtualUpcomingTab_betTitle__Oofi6',
    // Market/round tabs share one generic data-test-id; disambiguate by label text.
    tab: '[role="tab"]',
    oddButton: 'button[data-test-id^="odd-"]',
    oddLabel: '._label_r1b54_47',
    stakeInput: 'input[data-test-id="stake-input"]',
    placeBetButton: 'button[data-test-id="place-bet-button"]',
    balance: '.CurrencySelector_balanceMeasure__Y1cb3',
};

const retry = async (fn, retries = 3, delay = 1000) => {
    for (let i = 0; i < retries; i++) {
        try {
            return await fn();
        } catch (e) {
            if (i === retries - 1) throw e;
            await new Promise((res) => setTimeout(res, delay));
        }
    }
};

async function clickAt(page, x, y) {
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.up();
}

async function clickByText(page, text, { root = null, exact = true } = {}) {
    const box = await page.evaluate(
        ({ text, exact, root }) => {
            const scope = root ? document.querySelector(root) : document;
            if (!scope) return null;
            const els = Array.from(scope.querySelectorAll('*'));
            const el = els.find((e) => {
                const t = e.textContent.trim();
                return exact ? t === text : t.includes(text);
            });
            if (!el) return null;
            const clickable = el.closest('button,a,[role="tab"]') || el;
            const r = clickable.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        },
        { text, exact, root }
    );
    if (!box) throw new Error(`clickByText: element with text "${text}" not found`);
    await clickAt(page, box.x, box.y);
    return box;
}

export async function ensureActionPage(context, existingPage) {
    if (existingPage && !existingPage.isClosed() && existingPage.url().includes('betpawa.cm')) {
        return existingPage;
    }
    const page = await context.newPage();
    await page.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });
    return page;
}

async function ensureOverUnderTabActive(page) {
    const isActive = await page.evaluate(
        (sel) => {
            const tabs = Array.from(document.querySelectorAll(sel));
            const ou = tabs.find((t) => t.textContent.trim() === 'O/U');
            return ou ? ou.getAttribute('aria-selected') === 'true' : false;
        },
        SEL.tab
    );
    if (!isActive) {
        await clickByText(page, 'O/U');
        await page.waitForTimeout(500);
    }
}

async function getRowOneCardHandle(page, expectedFixtureName) {
    return page.evaluate(
        ({ cardSel, titleSel, expectedFixtureName }) => {
            const cards = Array.from(document.querySelectorAll(cardSel));
            if (!cards.length) return null;
            const card = cards[0];
            const title = card.querySelector(titleSel);
            const actualName = title ? title.textContent.trim() : null;
            return { actualName, matches: !expectedFixtureName || actualName === expectedFixtureName };
        },
        { cardSel: SEL.fixtureCard, titleSel: SEL.fixtureTitle, expectedFixtureName }
    );
}

async function clickUnder35OnRowOne(page) {
    const box = await page.evaluate(
        ({ cardSel, oddButtonSel, oddLabelSel }) => {
            const card = document.querySelector(cardSel);
            if (!card) return null;
            const buttons = Array.from(card.querySelectorAll(oddButtonSel));
            const btn = buttons.find((b) => {
                const lbl = b.querySelector(oddLabelSel);
                return lbl && lbl.textContent.trim() === 'Under 3.5';
            });
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        },
        { cardSel: SEL.fixtureCard, oddButtonSel: SEL.oddButton, oddLabelSel: SEL.oddLabel }
    );
    if (!box) throw new Error('Under 3.5 button not found on row-1 fixture');
    await clickAt(page, box.x, box.y);
}

// React-controlled input: setting .value directly and dispatching a plain
// 'input' event is ignored by React's change detection, so this uses the
// native HTMLInputElement setter before dispatching input/change.
async function setStake(page, stakeFcfa) {
    const ok = await page.evaluate(
        ({ sel, value }) => {
            const input = document.querySelector(sel);
            if (!input) return false;
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, String(value));
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        },
        { sel: SEL.stakeInput, value: stakeFcfa }
    );
    if (!ok) throw new Error('stake input not found');
}

async function takeScreenshot(page, name) {
    const screenshotDir = path.join('public', 'screenshots');
    if (!fs.existsSync(screenshotDir)) fs.mkdirSync(screenshotDir, { recursive: true });
    const screenshotPath = path.join(screenshotDir, `${name}-${Date.now()}.png`);
    await page.screenshot({ path: screenshotPath }).catch(() => {});
    return screenshotPath;
}

async function readBalance(page) {
    return page.evaluate((sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const m = el.textContent.match(/[\d.,]+/);
        return m ? parseFloat(m[0].replace(/,/g, '')) : null;
    }, SEL.balance);
}

/**
 * Places a flat-stake Under 3.5 (Full Time) bet on the row-1 fixture of the
 * currently displayed upcoming round.
 *
 * @returns {Promise<{success:boolean, dryRun?:boolean, fixtureName?:string, balanceBefore?:number, balanceAfter?:number}>}
 */
export async function placeUnder35Bet(page, { fixtureName, stakeFcfa, dryRun, log = console.log }) {
    try {
        // Building the betslip (navigation, tab, selection, stake) commits no
        // money and is safe to retry. The final submit click is NOT retried:
        // retrying a click-Place-Bet step after an ambiguous failure (e.g. the
        // confirmation check timing out) risks placing the same real-money bet
        // twice if the first click actually succeeded. Fail closed instead.
        await retry(async () => {
            await page.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });
            await page.waitForSelector(SEL.fixtureCard, { timeout: 15000 });

            await ensureOverUnderTabActive(page);

            const rowOne = await getRowOneCardHandle(page, fixtureName);
            if (!rowOne) throw new Error('row-1 fixture card not found');
            if (!rowOne.matches) {
                log(`row-1 fixture mismatch: expected "${fixtureName}", got "${rowOne.actualName}" (round likely moved on)`);
                throw new Error(`row-1 fixture mismatch: expected "${fixtureName}", got "${rowOne.actualName}"`);
            }

            await clickUnder35OnRowOne(page);
            await page.waitForSelector(SEL.stakeInput, { timeout: 10000 });
            await setStake(page, stakeFcfa);
        }, 3, 1000);

        if (dryRun) {
            log(`[DRY RUN] would place ${stakeFcfa} FCFA on "${fixtureName}" Under 3.5`);
            return { success: true, dryRun: true, fixtureName };
        }

        const balanceBefore = await readBalance(page);
        await page.click(SEL.placeBetButton);

        // Successful placement clears the betslip back to "Betslip is empty"
        // and decrements the balance; used together since neither alone was
        // validated against a real placed bet during recon (see plan's
        // verification step 3 — first live run is the real confirmation check).
        await page.waitForFunction(
            () => document.body.innerText.includes('Betslip is empty'),
            { timeout: 15000 }
        ).catch(() => {
            throw new Error('betslip did not clear after Place Bet click — placement unconfirmed, do NOT auto-retry');
        });
        const balanceAfter = await readBalance(page);

        return { success: true, fixtureName, balanceBefore, balanceAfter };
    } catch (err) {
        const screenshotPath = await takeScreenshot(page, 'virtual-bet-failure');
        log(`bet placement failed, screenshot saved: ${screenshotPath}`);
        throw err;
    }
}
