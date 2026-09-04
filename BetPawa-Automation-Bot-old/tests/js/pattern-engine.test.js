// Engine tests: cooldowns, gating and per-pattern isolation, with the
// browser and the clock stubbed out entirely.
// Run with: npm run test:js

import test from 'node:test';
import assert from 'node:assert/strict';

import { createPatternEngine, advanceCooldown } from '../../lib/pattern-engine.js';
import { createStreakPattern } from '../../lib/patterns/streak.js';

const bet = (selection) => ({ marketTab: 'O/U', selectionLabel: selection, market: 'Over/Under Full Time', selection });

const alwaysFires = (id, windowSize = 1) =>
    createStreakPattern({ id, name: id, windowSize, predicate: () => true, predicateLabel: 'always', bet: bet(`sel-${id}`) });
const neverFires = (id, windowSize = 1) =>
    createStreakPattern({ id, name: id, windowSize, predicate: () => false, predicateLabel: 'never', bet: bet(`sel-${id}`) });

/** Minimal in-memory stand-ins for the store, audit log and placement layer. */
function harness({ patterns, cooldownRounds = 3, maxBetsPerRun = 5, placeBetImpl } = {}) {
    const patternState = {};
    const placed = [];
    const audits = [];
    const logs = [];

    const store = {
        forPattern(id) {
            patternState[id] ??= { betRoundIds: [], cooldownRoundsRemaining: 0, cooldownLastCountedRoundId: null };
            return patternState[id];
        },
        hasAttempted(id, roundId) { return this.forPattern(id).betRoundIds.includes(roundId); },
        markAttempted(id, roundId) { this.forPattern(id).betRoundIds.push(roundId); },
        save() {},
    };
    const config = {
        maxBetsPerRun,
        dryRun: false,
        forPattern: () => ({ enabled: true, stakeFcfa: 5, cooldownRounds }),
    };
    const engine = createPatternEngine({
        patterns, config, store,
        auditLog: { append: (r) => audits.push(r) },
        log: (m) => logs.push(m),
        placeBet: placeBetImpl ?? (async (b) => { placed.push(b); return { success: true }; }),
    });

    let round = 0;
    /** Advance one round and let the engine act on it. */
    const tick = (sums = [1]) => engine.run({
        sums,
        settledRoundId: `settled-${round}`,
        bettingRound: { id: `betting-${round++}` },
        resolveFixture: async () => ({ name: 'ARS - MUN' }),
    });

    return { engine, store, patternState, placed, audits, logs, tick };
}

test('advanceCooldown counts each round once and is a no-op when not paused', () => {
    assert.deepEqual(advanceCooldown({ cooldownRoundsRemaining: 0 }, 'r1'), { paused: false, roundsRemaining: 0, counted: false });

    const state = { cooldownRoundsRemaining: 2, cooldownLastCountedRoundId: null };
    assert.deepEqual(advanceCooldown(state, 'r1'), { paused: true, roundsRemaining: 1, counted: true });
    // A repeat poll inside the same round must not consume a second slot.
    assert.deepEqual(advanceCooldown({ ...state, cooldownLastCountedRoundId: 'r1' }, 'r1'),
        { paused: true, roundsRemaining: 2, counted: false });
});

test('a firing pattern places exactly one bet, then goes quiet for its cooldown', async () => {
    const h = harness({ patterns: [alwaysFires('p')], cooldownRounds: 3 });

    await h.tick();
    assert.equal(h.placed.length, 1);
    assert.equal(h.placed[0].selectionLabel, 'sel-p');

    // The next three rounds are skipped even though the pattern still matches.
    await h.tick();
    await h.tick();
    await h.tick();
    assert.equal(h.placed.length, 1);

    await h.tick();
    assert.equal(h.placed.length, 2, 'betting resumes once the cooldown expires');
});

test('a repeat poll within the same round never places a second bet', async () => {
    const h = harness({ patterns: [alwaysFires('p')], cooldownRounds: 0 });
    const ctx = { sums: [1], settledRoundId: 's1', bettingRound: { id: 'b1' }, resolveFixture: async () => ({ name: 'ARS - MUN' }) };
    await h.engine.run(ctx);
    await h.engine.run(ctx);
    await h.engine.run(ctx);
    assert.equal(h.placed.length, 1);
});

test('a failed placement still starts the cooldown — it is unconfirmed, never retried', async () => {
    const h = harness({
        patterns: [alwaysFires('p')],
        cooldownRounds: 2,
        placeBetImpl: async () => { throw new Error('betslip did not clear'); },
    });
    await h.tick();
    assert.equal(h.audits.at(-1).success, false);
    assert.equal(h.patternState.p.cooldownRoundsRemaining, 2);
    await h.tick();
    assert.equal(h.audits.length, 1, 'no second attempt while paused');
});

test('two patterns firing on one round produce one bet, not a two-leg accumulator', async () => {
    const h = harness({ patterns: [alwaysFires('first'), alwaysFires('second')], cooldownRounds: 0 });
    await h.tick();
    assert.equal(h.placed.length, 1);
    assert.equal(h.placed[0].selectionLabel, 'sel-first', 'registry order breaks the tie');
    assert.ok(h.logs.some((l) => l.includes('[second]') && l.includes('already placed this round')));
    // The skipped pattern was never marked as attempted, so it stays eligible.
    assert.deepEqual(h.patternState.second.betRoundIds, []);
});

test("one pattern's cooldown does not mute another", async () => {
    const h = harness({ patterns: [alwaysFires('a'), neverFires('b')], cooldownRounds: 5 });
    await h.tick();                       // 'a' bets and goes on cooldown
    assert.equal(h.patternState.a.cooldownRoundsRemaining, 5);
    assert.equal(h.patternState.b.cooldownRoundsRemaining, 0, "'b' is untouched");
});

test('a pattern only sees the newest `windowSize` sums', async () => {
    // Also proves the engine accepts any object matching the Pattern
    // contract, not just ones built by createStreakPattern.
    const seen = [];
    const spy = {
        id: 'spy', name: 'spy', windowSize: 2, bet: bet('x'),
        evaluate: (sums) => { seen.push(sums); return false; },
        explain: () => '',
    };
    const h = harness({ patterns: [spy] });
    await h.tick([9, 8, 7, 6, 5]);
    assert.deepEqual(seen, [[6, 5]]);
});

test('a window shorter than the pattern needs is skipped silently', async () => {
    const h = harness({ patterns: [alwaysFires('wide', 5)] });
    await h.tick([1, 1, 1]);
    assert.equal(h.placed.length, 0);
    assert.equal(h.logs.length, 0);
});

test('the per-run cap stops placement without burning the round', async () => {
    const h = harness({ patterns: [alwaysFires('p')], cooldownRounds: 0, maxBetsPerRun: 2 });
    await h.tick();
    await h.tick();
    await h.tick();
    assert.equal(h.placed.length, 2);
    assert.ok(h.logs.some((l) => l.includes('cap reached')));
});

test('a missing fixture defers the bet to the next poll rather than burning the round', async () => {
    const h = harness({ patterns: [alwaysFires('p')], cooldownRounds: 0 });
    await h.engine.run({ sums: [1], settledRoundId: 's1', bettingRound: { id: 'b1' }, resolveFixture: async () => null });
    assert.equal(h.placed.length, 0);
    assert.deepEqual(h.patternState.p.betRoundIds, [], 'round not marked as attempted');

    await h.engine.run({ sums: [1], settledRoundId: 's1', bettingRound: { id: 'b1' }, resolveFixture: async () => ({ name: 'ARS - MUN' }) });
    assert.equal(h.placed.length, 1);
});

test('the audit record carries everything needed to reconcile against the bookmaker', async () => {
    const h = harness({ patterns: [alwaysFires('p', 2)] });
    await h.tick([1, 2]);
    const record = h.audits[0];
    assert.equal(record.pattern, 'p');
    assert.equal(record.fixture, 'ARS - MUN');
    assert.equal(record.selection, 'sel-p');
    assert.equal(record.stake, 5);
    assert.equal(record.success, true);
    assert.deepEqual(record.sums, [1, 2]);
    assert.ok(record.roundId);
});
