// DOM action layer: drives the real betting UI.
//
// Market-agnostic: a pattern supplies the on-screen market tab and odd label
// it wants ({ marketTab: 'O/U', selectionLabel: 'Over 2.5' }), and this module
// knows nothing about why. Selectors were captured live against the real page
// (see scripts/recon-virtual-betslip.js to re-derive them if betpawa.cm
// changes its markup).

import fs from 'fs';
import path from 'path';

export const VIRTUAL_SPORTS_URL = 'https://www.betpawa.cm/virtual-sports?virtualTab=upcoming&leagueId=7794';

const SEL = {
    // Repeating fixture-card container; DOM order === on-screen display order,
    // so index 0 is always "row 1" for whichever league filter is active.
    fixtureCard: '.VirtualUpcomingTab_betContainer__QzW5H',
    fixtureTitle: '.VirtualUpcomingTab_betTitle__Oofi6',
    tab: '[role="tab"]',
    // Round (matchday) tabs and market tabs both use role="tab"; only the
    // market/language ones carry a data-test-id, which is how they are told
    // apart below.
    marketTabTestId: 'auto-virtual-upcoming-tab-tabs-tab',
    oddButton: 'button[data-test-id^="odd-"]',
    oddLabel: '._label_r1b54_47',
    stakeInput: 'input[data-test-id="stake-input"]',
    placeBetButton: 'button[data-test-id="place-bet-button"]',
    betslipEmptyState: '[data-test-id="betslip-empty-state-img"]',
    balance: '.CurrencySelector_balanceMeasure__Y1cb3',
};

// Confirmed from the bot's own failure screenshots: a SUCCESSFUL placement
// renders a "Bet placed!" panel (green tick + "PLACE A NEW BET") in the
// betslip column. It does NOT fall back to the "Betslip is empty" state,
// which is what the first version of this module waited for — every one of
// the 9 bets it logged as failed had in fact been accepted.
const SUCCESS_TEXT = 'Bet placed!';
const NEW_BET_BUTTON_TEXT = 'PLACE A NEW BET';
const EMPTY_TEXT = 'Betslip is empty';
const CLEAR_BETSLIP_TEXT = 'Clear Betslip';

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

/** Centre of the first leaf element whose trimmed text equals `text`, or null. */
async function findTextBox(page, text) {
    return page.evaluate((text) => {
        const el = Array.from(document.querySelectorAll('*')).find(
            (e) => e.children.length === 0 && e.textContent.trim() === text
        );
        if (!el) return null;
        const clickable = el.closest('button,a,[role="tab"]') || el;
        const r = clickable.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    }, text);
}

async function clickText(page, text) {
    const box = await findTextBox(page, text);
    if (!box) throw new Error(`clickable element with text "${text}" not found`);
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

// The matchday selector reads e.g. "11 Live" | "12 21:50" | "Results". Bets
// must go on the upcoming matchday (the middle one) — never the in-play
// "Live" one. Loading VIRTUAL_SPORTS_URL with virtualTab=upcoming already
// selects it, but the site remembers the last tab across refreshes, so this
// asserts it rather than assuming.
async function ensureUpcomingRoundTabActive(page) {
    const state = await page.evaluate(
        ({ tabSel, marketTabTestId }) => {
            const roundTabs = Array.from(document.querySelectorAll(tabSel)).filter(
                (t) => t.getAttribute('data-test-id') !== marketTabTestId && !/^languageButton-/.test(t.getAttribute('data-test-id') || '')
            );
            const upcoming = roundTabs.find((t) => {
                const text = t.textContent.trim();
                return !/live$/i.test(text) && !/^results$/i.test(text);
            });
            if (!upcoming) return { found: false, tabs: roundTabs.map((t) => t.textContent.trim()) };
            const r = upcoming.getBoundingClientRect();
            return {
                found: true,
                label: upcoming.textContent.trim(),
                selected: upcoming.getAttribute('aria-selected') === 'true',
                box: { x: r.x + r.width / 2, y: r.y + r.height / 2 },
            };
        },
        { tabSel: SEL.tab, marketTabTestId: SEL.marketTabTestId }
    );
    if (!state.found) throw new Error(`upcoming matchday tab not found (round tabs seen: ${JSON.stringify(state.tabs)})`);
    if (!state.selected) {
        await clickAt(page, state.box.x, state.box.y);
        await page.waitForTimeout(500);
    }
    return state.label;
}

async function ensureMarketTabActive(page, marketTab) {
    const state = await page.evaluate(
        ({ tabSel, marketTab }) => {
            const tab = Array.from(document.querySelectorAll(tabSel)).find((t) => t.textContent.trim() === marketTab);
            if (!tab) return { found: false };
            const r = tab.getBoundingClientRect();
            return {
                found: true,
                selected: tab.getAttribute('aria-selected') === 'true',
                box: { x: r.x + r.width / 2, y: r.y + r.height / 2 },
            };
        },
        { tabSel: SEL.tab, marketTab }
    );
    if (!state.found) throw new Error(`market tab "${marketTab}" not found`);
    if (!state.selected) {
        await clickAt(page, state.box.x, state.box.y);
        await page.waitForTimeout(500);
    }
}

function readBetslipState(page) {
    return page.evaluate(
        ({ stakeSel, successText }) => ({
            // The stake input exists only once the betslip holds at least one
            // leg, which makes it the reliable "has a selection" signal.
            hasSelections: !!document.querySelector(stakeSel),
            showsSuccess: document.body.innerText.includes(successText),
        }),
        { stakeSel: SEL.stakeInput, successText: SUCCESS_TEXT }
    );
}

// The betslip is shared, persistent state across tabs and page loads. Any
// selection left behind — by a previous pattern, a previous run, or a retried
// attempt — would be submitted together with ours as a multi-leg accumulator,
// which is a completely different (and losing-by-default) bet. So every
// placement starts from a provably empty betslip.
//
// "Holds a selection" is judged by the stake input, which only exists once the
// betslip has at least one leg. That is positive evidence, so a betslip column
// that simply has not rendered yet reads as "nothing to clear" and the flow
// continues — rather than a "not empty" test, which would abort on a slow
// render even though there was never anything there.
async function resetBetslip(page) {
    // Best-effort wait for the column to reach a determinate state; the checks
    // below are correct either way, so a timeout here is not an error.
    await page
        .waitForFunction(
            ({ emptySel, stakeSel, successText }) =>
                !!document.querySelector(emptySel) ||
                !!document.querySelector(stakeSel) ||
                document.body.innerText.includes(successText),
            { emptySel: SEL.betslipEmptyState, stakeSel: SEL.stakeInput, successText: SUCCESS_TEXT },
            { timeout: 5000 }
        )
        .catch(() => {});

    let state = await readBetslipState(page);

    // A previous placement's "Bet placed!" panel is covering the betslip.
    if (state.showsSuccess) {
        await clickText(page, NEW_BET_BUTTON_TEXT).catch(() => {});
        await page.waitForTimeout(500);
        state = await readBetslipState(page);
    }

    if (!state.hasSelections) return;

    if (!(await findTextBox(page, CLEAR_BETSLIP_TEXT))) {
        throw new Error('betslip holds a selection but "Clear Betslip" was not found — refusing to add a second leg');
    }
    await clickText(page, CLEAR_BETSLIP_TEXT);
    await page.waitForTimeout(500);

    if ((await readBetslipState(page)).hasSelections) {
        throw new Error('betslip still holds a selection after clearing — refusing to place a multi-leg bet');
    }
}

async function readRowOneFixtureName(page) {
    return page.evaluate(
        ({ cardSel, titleSel }) => {
            const card = document.querySelector(cardSel);
            if (!card) return null;
            return card.querySelector(titleSel)?.textContent.trim() ?? null;
        },
        { cardSel: SEL.fixtureCard, titleSel: SEL.fixtureTitle }
    );
}

async function clickSelectionOnRowOne(page, selectionLabel) {
    const box = await page.evaluate(
        ({ cardSel, oddButtonSel, oddLabelSel, selectionLabel }) => {
            const card = document.querySelector(cardSel);
            if (!card) return null;
            const buttons = Array.from(card.querySelectorAll(oddButtonSel));
            // Primary: the dedicated label node. Fallback: the button's own
            // text, which renders as label+odds ("Over 2.51.85") — this keeps
            // working if the hashed CSS-module class name changes.
            const btn =
                buttons.find((b) => b.querySelector(oddLabelSel)?.textContent.trim() === selectionLabel) ||
                buttons.find((b) => b.textContent.trim().startsWith(selectionLabel));
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        },
        { cardSel: SEL.fixtureCard, oddButtonSel: SEL.oddButton, oddLabelSel: SEL.oddLabel, selectionLabel }
    );
    if (!box) throw new Error(`"${selectionLabel}" button not found on the row-1 fixture`);
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
    const dir = path.join('public', 'screenshots');
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const screenshotPath = path.join(dir, `${name}-${Date.now()}.png`);
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
 * Places a single-leg, flat-stake bet on the row-1 fixture of the upcoming
 * round.
 *
 * @param {import('playwright').Page} page
 * @param {{ fixtureName: string, marketTab: string, selectionLabel: string,
 *           stakeFcfa: number, dryRun?: boolean, log?: (m:string)=>void }} opts
 * @returns {Promise<{success:boolean, dryRun?:boolean, fixtureName:string, matchday?:string,
 *                    balanceBefore?:number, balanceAfter?:number}>}
 */
export async function placeBet(page, { fixtureName, marketTab, selectionLabel, stakeFcfa, dryRun = false, log = console.log }) {
    try {
        let matchday;
        // Building the betslip (navigation, tabs, clearing, selection, stake)
        // commits no money and is safe to retry. The final submit click is NOT
        // retried: retrying after an ambiguous failure risks placing the same
        // real-money bet twice if the first click actually succeeded. Fail
        // closed instead.
        await retry(async () => {
            await page.goto(VIRTUAL_SPORTS_URL, { waitUntil: 'domcontentloaded' });
            await page.waitForSelector(SEL.fixtureCard, { timeout: 15000 });

            matchday = await ensureUpcomingRoundTabActive(page);
            await ensureMarketTabActive(page, marketTab);
            await resetBetslip(page);

            const actualName = await readRowOneFixtureName(page);
            if (!actualName) throw new Error('row-1 fixture card not found');
            if (actualName !== fixtureName) {
                throw new Error(`row-1 fixture mismatch: expected "${fixtureName}", got "${actualName}" (round likely moved on)`);
            }

            await clickSelectionOnRowOne(page, selectionLabel);
            await page.waitForSelector(SEL.stakeInput, { timeout: 10000 });
            await setStake(page, stakeFcfa);
        }, 3, 1000);

        if (dryRun) {
            log(`[DRY RUN] would place ${stakeFcfa} FCFA on "${fixtureName}" ${marketTab} ${selectionLabel} (matchday ${matchday})`);
            // A dry run exercises every selector and every step except the
            // final submit, so it is the way to validate this module against
            // the live site. Undo the selection it built so it leaves the
            // shared betslip exactly as it found it.
            await resetBetslip(page).catch((e) => log(`[DRY RUN] could not clear the betslip afterwards: ${e.message}`));
            return { success: true, dryRun: true, fixtureName, matchday };
        }

        const balanceBefore = await readBalance(page);
        await page.click(SEL.placeBetButton);

        // A confirmed placement renders the "Bet placed!" panel. The empty
        // state is accepted too, purely as a belt-and-braces fallback in case
        // the site ever changes that copy.
        await page
            .waitForFunction(
                ({ successText, emptyText }) =>
                    document.body.innerText.includes(successText) || document.body.innerText.includes(emptyText),
                { successText: SUCCESS_TEXT, emptyText: EMPTY_TEXT },
                { timeout: 20000 }
            )
            .catch(() => {
                throw new Error('no placement confirmation after Place Bet click — placement unconfirmed, do NOT auto-retry');
            });
        const balanceAfter = await readBalance(page);

        return { success: true, fixtureName, matchday, balanceBefore, balanceAfter };
    } catch (err) {
        const screenshotPath = await takeScreenshot(page, 'virtual-bet-failure');
        log(`bet placement failed, screenshot saved: ${screenshotPath}`);
        throw err;
    }
}
