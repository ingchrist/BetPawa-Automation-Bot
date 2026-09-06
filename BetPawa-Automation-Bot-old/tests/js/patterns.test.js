// Pattern-registry and pattern-evaluation tests.
// Run with: npm run test:js

import test from 'node:test';
import assert from 'node:assert/strict';

import { loadConfig } from '../../lib/config.js';
import { ALL_PATTERNS, getEnabledPatterns, maxWindowSize } from '../../lib/patterns/index.js';
import { createStreakPattern } from '../../lib/patterns/streak.js';
import highScoringPair from '../../lib/patterns/high-scoring-pair.js';
import lowScoringStreak from '../../lib/patterns/low-scoring-streak.js';
// low-scoring-trio is currently NOT registered (see lib/patterns/index.js).
// Its module is imported directly here so its rules stay under test while it
// is switched off, ready for the day it is put back in ALL_PATTERNS.
import lowScoringTrio from '../../lib/patterns/low-scoring-trio.js';

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

test('low-scoring-trio fires on three rounds of <=2 goals and bets Over 2.5', () => {
    // The real observed sequence, logs/virtual-pattern-bot-2026-09-04.log:
    //   (ARS - COV) sum=1, (ARS - EVE) sum=2, (AST - CHE) sum=1
    //   -> (ARS - MUN) sum=3, so Over 2.5 would have won.
    assert.equal(lowScoringTrio.evaluate([1, 2, 1]), true);
    assert.equal(lowScoringTrio.evaluate([0, 0, 0]), true);
    assert.equal(lowScoringTrio.evaluate([2, 2, 2]), true);
    assert.equal(lowScoringTrio.bet.marketTab, 'O/U');
    assert.equal(lowScoringTrio.bet.selectionLabel, 'Over 2.5');
});

test('low-scoring-trio does not fire when any round in the window breaks the streak', () => {
    assert.equal(lowScoringTrio.evaluate([3, 1, 2]), false); // oldest breaks it
    assert.equal(lowScoringTrio.evaluate([1, 3, 2]), false); // middle breaks it
    assert.equal(lowScoringTrio.evaluate([1, 2, 3]), false); // newest breaks it
    assert.equal(lowScoringTrio.evaluate([]), false);
    assert.equal(lowScoringTrio.evaluate([1, 1]), false);    // partial window
});

test('low-scoring-trio strictly subsumes low-scoring-streak, and they want the same bet', () => {
    // This is why the engine coalesces duplicate selections rather than
    // placing twice: whenever the 5-round streak fires, so does the 3-round
    // one, for the identical Over 2.5 on the identical fixture.
    for (let i = 0; i < 3 ** 5; i++) {
        const w = Array.from({ length: 5 }, (_, k) => Math.floor(i / 3 ** k) % 3);
        if (lowScoringStreak.evaluate(w)) {
            assert.ok(lowScoringTrio.evaluate(w.slice(-3)), `trio should also fire on ${w}`);
        }
    }
    // ...but not the other way round: a low trio preceded by a high round.
    assert.equal(lowScoringTrio.evaluate([1, 1, 1]), true);
    assert.equal(lowScoringStreak.evaluate([5, 4, 1, 1, 1]), false);

    assert.equal(lowScoringTrio.bet.market, lowScoringStreak.bet.market);
    assert.equal(lowScoringTrio.bet.selection, lowScoringStreak.bet.selection);
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

test('high-scoring-pair can never fire alongside either low-scoring pattern', () => {
    // Sums cannot be both >= 4 and <= 2, so the Under 3.5 pattern and the
    // Over 2.5 ones can never contend for the same round.
    for (let a = 0; a <= 9; a++) {
        for (let b = 0; b <= 9; b++) {
            for (let c = 0; c <= 9; c++) {
                const wide = [a, b, c, b, c];
                const pair = highScoringPair.evaluate(wide.slice(-2));
                assert.ok(!(pair && lowScoringStreak.evaluate(wide)));
                assert.ok(!(pair && lowScoringTrio.evaluate(wide.slice(-3))));
            }
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
    assert.equal(maxWindowSize([lowScoringTrio]), 3);
});

test('patterns can be selected, and a single one disabled, from config', () => {
    assert.deepEqual(getEnabledPatterns(cfg()).map((p) => p.id),
        ['high-scoring-pair', 'low-scoring-streak']);
    assert.deepEqual(
        getEnabledPatterns(cfg({}, ['--patterns=low-scoring-streak'])).map((p) => p.id),
        ['low-scoring-streak']
    );
    assert.deepEqual(
        getEnabledPatterns(cfg({ VIRTUAL_HIGH_SCORING_PAIR_ENABLED: 'false' })).map((p) => p.id),
        ['low-scoring-streak']
    );
    assert.deepEqual(
        getEnabledPatterns(cfg({ VIRTUAL_LOW_SCORING_STREAK_ENABLED: 'false' })).map((p) => p.id),
        ['high-scoring-pair']
    );
});

test('low-scoring-trio is unregistered, so no config can switch it back on', () => {
    // Turning it off was a deliberate operator decision, and un-registering it
    // is the switch. Nothing an operator can put in .env or on the CLI should
    // reach it — only editing lib/patterns/index.js can.
    assert.ok(!ALL_PATTERNS.some((p) => p.id === 'low-scoring-trio'));
    assert.ok(!getEnabledPatterns(cfg()).some((p) => p.id === 'low-scoring-trio'));
    assert.ok(!getEnabledPatterns(cfg({ VIRTUAL_LOW_SCORING_TRIO_ENABLED: 'true' }))
        .some((p) => p.id === 'low-scoring-trio'));
    assert.throws(() => getEnabledPatterns(cfg({}, ['--patterns=low-scoring-trio'])), /unknown pattern id/);
});

test('an unknown pattern id is a startup error, not a silent no-op', () => {
    assert.throws(() => getEnabledPatterns(cfg({}, ['--patterns=typo'])), /unknown pattern id/);
});

test('stake and cooldown fall back to the global default, and can be overridden per pattern', () => {
    const c = cfg({ VIRTUAL_STAKE_FCFA: '20', VIRTUAL_LOW_SCORING_STREAK_STAKE_FCFA: '100' });
    assert.equal(c.forPattern('high-scoring-pair').stakeFcfa, 20);
    assert.equal(c.forPattern('low-scoring-streak').stakeFcfa, 100);
    assert.equal(c.forPattern('low-scoring-trio').stakeFcfa, 20, 'falls back to the global stake');
    // The documented default: 5 FCFA, overridable per pattern to any amount.
    assert.equal(cfg().forPattern('low-scoring-trio').stakeFcfa, 5);
    assert.equal(cfg({ VIRTUAL_LOW_SCORING_TRIO_STAKE_FCFA: '250' }).forPattern('low-scoring-trio').stakeFcfa, 250);
    assert.equal(cfg({}, ['--stake=75']).forPattern('low-scoring-trio').stakeFcfa, 75);

    const d = cfg({ VIRTUAL_COOLDOWN_ROUNDS: '4', VIRTUAL_LOW_SCORING_STREAK_COOLDOWN_ROUNDS: '0' });
    assert.equal(d.forPattern('high-scoring-pair').cooldownRounds, 4);
    assert.equal(d.forPattern('low-scoring-streak').cooldownRounds, 0);
});

test('a malformed tuning value falls back to the default instead of becoming NaN', () => {
    // A NaN cap or cooldown would silently disable a safety gate.
    const c = cfg({ VIRTUAL_STAKE_FCFA: 'abc', VIRTUAL_MAX_BETS_PER_RUN: '', VIRTUAL_COOLDOWN_ROUNDS: '-1' });
    assert.equal(c.stakeFcfa, 5);
    assert.equal(c.maxBetsPerRun, 5);
    assert.equal(c.cooldownRounds, 1);
});
