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
/** Fires on everything and wants the SAME selection as another pattern. */
const alwaysFiresWanting = (id, selection, windowSize = 1) =>
    createStreakPattern({ id, name: id, windowSize, predicate: () => true, predicateLabel: 'always', bet: bet(selection) });
const neverFires = (id, windowSize = 1) =>
    createStreakPattern({ id, name: id, windowSize, predicate: () => false, predicateLabel: 'never', bet: bet(`sel-${id}`) });

/** Minimal in-memory stand-ins for the store, audit log and placement layer. */
function harness({ patterns, cooldownRounds = 3, maxBetsPerRun = 5, placeBetImpl, stakes = {} } = {}) {
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
        forPattern: (id) => ({ enabled: true, stakeFcfa: stakes[id] ?? 5, cooldownRounds }),
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

// --- several patterns firing on the same round --------------------------

test('two patterns wanting DIFFERENT selections both bet, strictly one after the other', async () => {
    const order = [];
    const h = harness({
        patterns: [alwaysFires('first'), alwaysFires('second')],
        cooldownRounds: 0,
        // Records entry and exit, so an overlap would show up as
        // start-second before end-first — i.e. two slips open at once.
        placeBetImpl: async (b) => {
            order.push(`start-${b.selectionLabel}`);
            await new Promise((r) => setTimeout(r, 5));
            order.push(`end-${b.selectionLabel}`);
            return { success: true };
        },
    });

    await h.tick();
    assert.deepEqual(order, ['start-sel-first', 'end-sel-first', 'start-sel-second', 'end-sel-second']);
    assert.deepEqual(h.audits.map((a) => [a.pattern, a.selection]), [['first', 'sel-first'], ['second', 'sel-second']]);
    assert.ok(h.logs.some((l) => l.includes('[second]') && l.includes('separate single')));
});

test('two patterns wanting the SAME selection place it once, at one stake', async () => {
    // The real case: low-scoring-trio subsumes low-scoring-streak and both
    // bet Over 2.5. Two placements would be one bet at double stake.
    const h = harness({
        patterns: [alwaysFiresWanting('streak', 'Over 2.5'), alwaysFiresWanting('trio', 'Over 2.5')],
        cooldownRounds: 0,
        stakes: { streak: 5, trio: 40 },
    });

    await h.tick();
    assert.equal(h.placed.length, 1, 'one placement, not two');
    assert.equal(h.placed[0].stakeFcfa, 5, "the earlier-listed pattern's stake is the one used");
    assert.ok(h.logs.some((l) => l.includes('[trio]') && l.includes('not staking it twice')));

    // The coalesced pattern is still fully accounted for: marked as attempted
    // so a repeat poll stays quiet, audited as having moved no money of its
    // own, and paused as if it had placed.
    assert.deepEqual(h.patternState.trio.betRoundIds, ['betting-0']);
    const coalesced = h.audits.find((a) => a.pattern === 'trio');
    assert.equal(coalesced.placed, false);
    assert.equal(coalesced.coalescedInto, 'streak');
    assert.equal(coalesced.stake, 0);
    assert.equal(coalesced.requestedStake, 40);
    assert.equal(coalesced.success, true);
});

test('a duplicate selection is not re-placed after an UNCONFIRMED failure', async () => {
    // A failed placement may still be on at the bookmaker, so a second
    // pattern asking for the same selection is a retry in disguise.
    const h = harness({
        patterns: [alwaysFiresWanting('streak', 'Over 2.5'), alwaysFiresWanting('trio', 'Over 2.5')],
        cooldownRounds: 0,
        placeBetImpl: async () => { throw new Error('no placement confirmation'); },
    });

    await h.tick();
    assert.equal(h.audits.length, 2);
    const coalesced = h.audits.find((a) => a.pattern === 'trio');
    assert.equal(coalesced.placed, false);
    assert.equal(coalesced.success, false, 'not reported as a bet that is on');
    assert.ok(h.logs.some((l) => l.includes('[trio]') && l.includes('unconfirmed')));
});

test("a failed placement does not stop a DIFFERENT selection going on afterwards", async () => {
    const h = harness({
        patterns: [alwaysFires('first'), alwaysFires('second')],
        cooldownRounds: 0,
        placeBetImpl: async (b) => {
            if (b.selectionLabel === 'sel-first') throw new Error('betslip did not clear');
            return { success: true };
        },
    });

    await h.tick();
    assert.deepEqual(h.audits.map((a) => [a.pattern, a.success]), [['first', false], ['second', true]]);
});

test('a repeat poll never re-places any of a multi-bet round', async () => {
    const h = harness({ patterns: [alwaysFires('first'), alwaysFires('second')], cooldownRounds: 0 });
    const ctx = { sums: [1], settledRoundId: 's1', bettingRound: { id: 'b1' }, resolveFixture: async () => ({ name: 'ARS - MUN' }) };
    await h.engine.run(ctx);
    await h.engine.run(ctx);
    assert.equal(h.placed.length, 2);
});

test('the per-run cap counts bets across patterns within a single round', async () => {
    const h = harness({
        patterns: [alwaysFires('a'), alwaysFires('b'), alwaysFires('c')],
        cooldownRounds: 0,
        maxBetsPerRun: 2,
    });
    await h.tick();
    assert.equal(h.placed.length, 2);
    assert.ok(h.logs.some((l) => l.includes('[c]') && l.includes('cap reached')));
    assert.deepEqual(h.patternState.c.betRoundIds, [], 'the capped pattern stays eligible');
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
