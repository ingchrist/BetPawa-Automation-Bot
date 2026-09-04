// Pattern-registry and pattern-evaluation tests.
// Run with: npm run test:js

import test from 'node:test';
import assert from 'node:assert/strict';

import { loadConfig } from '../../lib/config.js';
import { ALL_PATTERNS, getEnabledPatterns, maxWindowSize } from '../../lib/patterns/index.js';
import { createStreakPattern } from '../../lib/patterns/streak.js';
import highScoringPair from '../../lib/patterns/high-scoring-pair.js';
import lowScoringStreak from '../../lib/patterns/low-scoring-streak.js';

const cfg = (env = {}, argv = []) => loadConfig({ argv, env });

test('every registered pattern satisfies the Pattern contract', () => {
    for (const p of ALL_PATTERNS) {
        assert.ok(p.id && typeof p.id === 'string', 'id');
        assert.ok(p.name && typeof p.name === 'string', `${p.id}: name`);
        assert.ok(Number.isInteger(p.windowSize) && p.windowSize > 0, `${p.id}: windowSize`);
        assert.equal(typeof p.evaluate, 'function', `${p.id}: evaluate`);
        assert.equal(typeof p.explain, 'function', `${p.id}: explain`);
        for (const key of ['marketTab', 'selectionLabel', 'market', 'selection']) {
            assert.ok(p.bet[key], `${p.id}: bet.${key}`);
        }
    }
});

test('pattern ids are unique', () => {
    const ids = ALL_PATTERNS.map((p) => p.id);
    assert.equal(new Set(ids).size, ids.length);
});

test('high-scoring-pair fires on two rounds of 4+ goals and bets Under 3.5', () => {
    assert.equal(highScoringPair.evaluate([4, 4]), true);
    assert.equal(highScoringPair.evaluate([5, 7]), true);
    assert.equal(highScoringPair.evaluate([4, 3]), false);
    assert.equal(highScoringPair.evaluate([3, 4]), false);
    assert.equal(highScoringPair.bet.selectionLabel, 'Under 3.5');
});

test('low-scoring-streak fires on five rounds of <=2 goals and bets Over 2.5', () => {
    // The real observed sequence, logs/virtual-pattern-bot-2026-09-04.log 18:06-18:26.
    assert.equal(lowScoringStreak.evaluate([2, 1, 1, 2, 1]), true);
    assert.equal(lowScoringStreak.evaluate([0, 0, 0, 0, 0]), true);
    assert.equal(lowScoringStreak.evaluate([2, 2, 2, 2, 2]), true);
    assert.equal(lowScoringStreak.bet.marketTab, 'O/U');
    assert.equal(lowScoringStreak.bet.selectionLabel, 'Over 2.5');
});

test('low-scoring-streak does not fire when any round in the window breaks the streak', () => {
    assert.equal(lowScoringStreak.evaluate([3, 1, 1, 2, 1]), false); // oldest breaks it
    assert.equal(lowScoringStreak.evaluate([2, 1, 3, 2, 1]), false); // middle breaks it
    assert.equal(lowScoringStreak.evaluate([2, 1, 1, 2, 3]), false); // newest breaks it
});

test('a partial window never fires — `every` must not be vacuously true', () => {
    assert.equal(lowScoringStreak.evaluate([]), false);
    assert.equal(lowScoringStreak.evaluate([1, 1, 1, 1]), false);
    assert.equal(highScoringPair.evaluate([]), false);
    assert.equal(highScoringPair.evaluate([9]), false);
});

test('an over-long window is rejected rather than silently truncated', () => {
    // The engine slices to windowSize; this guards the contract itself.
    assert.equal(highScoringPair.evaluate([4, 4, 4]), false);
});

test('the two patterns are mutually exclusive', () => {
    // sums cannot be both >= 4 and <= 2, so they can never fire on the same window.
    for (let a = 0; a <= 9; a++) {
        for (let b = 0; b <= 9; b++) {
            const wide = [a, b, a, b, a];
            assert.ok(!(highScoringPair.evaluate(wide.slice(-2)) && lowScoringStreak.evaluate(wide)));
        }
    }
});

test('createStreakPattern rejects a nonsensical window size', () => {
    const spec = { id: 'x', name: 'x', predicate: () => true, predicateLabel: '', bet: {} };
    assert.throws(() => createStreakPattern({ ...spec, windowSize: 0 }), /positive integer/);
    assert.throws(() => createStreakPattern({ ...spec, windowSize: 2.5 }), /positive integer/);
});

test('history depth is driven by the widest enabled pattern', () => {
    assert.equal(maxWindowSize(ALL_PATTERNS), 5);
    assert.equal(maxWindowSize([highScoringPair]), 2);
});

test('patterns can be selected, and a single one disabled, from config', () => {
    assert.deepEqual(getEnabledPatterns(cfg()).map((p) => p.id), ['high-scoring-pair', 'low-scoring-streak']);
    assert.deepEqual(getEnabledPatterns(cfg({}, ['--patterns=low-scoring-streak'])).map((p) => p.id), ['low-scoring-streak']);
    assert.deepEqual(
        getEnabledPatterns(cfg({ VIRTUAL_HIGH_SCORING_PAIR_ENABLED: 'false' })).map((p) => p.id),
        ['low-scoring-streak']
    );
});

test('an unknown pattern id is a startup error, not a silent no-op', () => {
    assert.throws(() => getEnabledPatterns(cfg({}, ['--patterns=typo'])), /unknown pattern id/);
});

test('stake and cooldown fall back to the global default, and can be overridden per pattern', () => {
    const c = cfg({ VIRTUAL_STAKE_FCFA: '20', VIRTUAL_LOW_SCORING_STREAK_STAKE_FCFA: '100' });
    assert.equal(c.forPattern('high-scoring-pair').stakeFcfa, 20);
    assert.equal(c.forPattern('low-scoring-streak').stakeFcfa, 100);

    const d = cfg({ VIRTUAL_COOLDOWN_ROUNDS: '4', VIRTUAL_LOW_SCORING_STREAK_COOLDOWN_ROUNDS: '0' });
    assert.equal(d.forPattern('high-scoring-pair').cooldownRounds, 4);
    assert.equal(d.forPattern('low-scoring-streak').cooldownRounds, 0);
});

test('a malformed tuning value falls back to the default instead of becoming NaN', () => {
    // A NaN cap or cooldown would silently disable a safety gate.
    const c = cfg({ VIRTUAL_STAKE_FCFA: 'abc', VIRTUAL_MAX_BETS_PER_RUN: '', VIRTUAL_COOLDOWN_ROUNDS: '-1' });
    assert.equal(c.stakeFcfa, 5);
    assert.equal(c.maxBetsPerRun, 5);
    assert.equal(c.cooldownRounds, 3);
});
