// Pure-domain tests for lib/betpawa/rounds.js — no browser, no network.
// Run with: npm run test:js

import test from 'node:test';
import assert from 'node:assert/strict';

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
    FINALITY_BUFFER_MS,
} from '../../lib/betpawa/rounds.js';

const ROUND_MS = 5 * 60 * 1000;
const mkRound = (id, startMs) => ({
    id: String(id),
    tradingTime: { start: new Date(startMs).toISOString(), end: new Date(startMs + ROUND_MS).toISOString() },
});
// Rounds 0..8, each 5 minutes long, starting at t=0.
const rounds = Array.from({ length: 9 }, (_, i) => mkRound(i, i * ROUND_MS));

test('sortRoundsAsc orders by trading start, not by id', () => {
    // Round ids are not chronological across seasons: a later season can carry
    // numerically lower ids than the season still running.
    const unsorted = [mkRound(999, 2 * ROUND_MS), mkRound(1, 0), mkRound(500, ROUND_MS)];
    assert.deepEqual(sortRoundsAsc(unsorted).map((r) => r.id), ['1', '500', '999']);
});

test('the betting round is the started-but-unfinished one, not the next to start', () => {
    const now = 7.5 * ROUND_MS; // mid-way through round 7
    const { index } = findNextUpcomingRound(rounds, now);
    assert.equal(index, 8);
    assert.equal(getBettingRound(rounds, index).id, '7');
});

test('getSettledWindow returns the settled run ending two rounds before the next', () => {
    const now = 7.5 * ROUND_MS;
    const { index } = findNextUpcomingRound(rounds, now);
    assert.deepEqual(getSettledWindow(rounds, index, 5, now).map((r) => r.id), ['2', '3', '4', '5', '6']);
    assert.deepEqual(getSettledWindow(rounds, index, 2, now).map((r) => r.id), ['5', '6']);
});

test('getSettledWindow truncates rather than failing when history is short', () => {
    const now = 2.5 * ROUND_MS;
    const { index } = findNextUpcomingRound(rounds, now);
    // Only rounds 0 and 1 have settled; a 5-wide pattern simply has to wait.
    assert.deepEqual(getSettledWindow(rounds, index, 5, now).map((r) => r.id), ['0', '1']);
});

test('a round is not settled until the finality buffer has elapsed', () => {
    // Round 6 ends at t=35min. The newest candidate is only usable after the
    // buffer, because a full-time score can post provisionally and be corrected.
    const justAfterClose = 7 * ROUND_MS + 1000;
    const { index } = findNextUpcomingRound(rounds, justAfterClose);
    assert.deepEqual(getSettledWindow(rounds, index, 5, justAfterClose), []);

    const afterBuffer = 7 * ROUND_MS + FINALITY_BUFFER_MS + 1000;
    assert.equal(getSettledWindow(rounds, index, 5, afterBuffer).at(-1).id, '6');
});

test('getSettledWindow is empty when nothing has been played yet', () => {
    assert.deepEqual(getSettledWindow(rounds, 0, 5, 0), []);
    assert.deepEqual(getSettledWindow(rounds, 1, 5, 0), []);
});

// --- fixture / score extraction -------------------------------------------

const fixture = (name, competitionId, ftHome, ftAway, htHome = 0, htAway = 0) => ({
    name,
    competition: { id: competitionId },
    results: {
        participantPeriodResults: [
            { participant: { type: 'HOME' }, periodResults: [
                { period: { slug: 'FULL_TIME_EXCLUDING_OVERTIME' }, result: ftHome },
                { period: { slug: 'FIRST_HALF' }, result: htHome },
            ] },
            { participant: { type: 'AWAY' }, periodResults: [
                { period: { slug: 'FULL_TIME_EXCLUDING_OVERTIME' }, result: ftAway },
                { period: { slug: 'FIRST_HALF' }, result: htAway },
            ] },
        ],
    },
});

test('row 1 is the alphabetically-first fixture of the target league, not API order', () => {
    const responses = [
        fixture('TOT - BRE', '7794', 1, 1),
        fixture('AAA - ZZZ', '9999', 0, 0), // different league — must be ignored
        fixture('ARS - BHA', '7794', 2, 0),
    ];
    assert.equal(getRowOneFixture(responses, '7794').name, 'ARS - BHA');
});

test('getRowOneFixture returns null when the league has no fixtures', () => {
    assert.equal(getRowOneFixture([fixture('AAA - ZZZ', '9999', 0, 0)], '7794'), null);
});

test('a fixture is finalized only once both sides have a full-time score', () => {
    const half = { name: 'A - B', competition: { id: '7794' }, results: { participantPeriodResults: [
        { participant: { type: 'HOME' }, periodResults: [{ period: { slug: 'FULL_TIME_EXCLUDING_OVERTIME' }, result: 2 }] },
    ] } };
    assert.equal(isFixtureFinalized(half), false);
    assert.equal(getFullTimeScore(half), null);
    assert.equal(isFixtureFinalized(fixture('A - B', '7794', 2, 1)), true);
    assert.deepEqual(getFullTimeScore(fixture('A - B', '7794', 2, 1)), { home: 2, away: 1, sum: 3 });
});

test('display mirrors the site row: (HT) FT', () => {
    const fx = fixture('ARS - MUN', '7794', 3, 0, 1, 0);
    const score = getScoreDisplay(fx);
    assert.equal(score.sum, 3);
    assert.equal(formatFixtureLine(fx, score), '(ARS - MUN)   (1 - 0) 3 - 0');
});

test('countdown mirrors the site and never goes negative', () => {
    assert.equal(formatCountdown(mkRound(1, 222_000), 0), '03:42');
    assert.equal(formatCountdown(mkRound(1, 0), 999_000), '00:00');
});
