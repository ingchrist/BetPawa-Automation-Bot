// Engine tests: life-cycle gating, multi-pattern placement and per-pattern
// isolation, with the browser and the clock stubbed out entirely.
// The cycle rule itself is pinned separately in pattern-cycle.test.js.
// Run with: npm run test:js

import test from 'node:test';
import assert from 'node:assert/strict';

import { createPatternEngine } from '../../lib/pattern-engine.js';
import { createStreakPattern } from '../../lib/patterns/streak.js';

const bet = (selection) => ({ marketTab: 'O/U', selectionLabel: selection, market: 'Over/Under Full Time', selection });

const alwaysFires = (id, windowSize = 1) =>
    createStreakPattern({ id, name: id, windowSize, predicate: () => true, predicateLabel: 'always', bet: bet(`sel-${id}`) });
/** Fires on everything and wants the SAME selection as another pattern. */
const alwaysFiresWanting = (id, selection, windowSize = 1) =>
    createStreakPattern({ id, name: id, windowSize, predicate: () => true, predicateLabel: 'always', bet: bet(selection) });
const neverFires = (id, windowSize = 1) =>
    createStreakPattern({ id, name: id, windowSize, predicate: () => false, predicateLabel: 'never', bet: bet(`sel-${id}`) });

// How much settled history the real bot hands the engine (its widest window
// plus LATE_RESULT_SLACK), so the stub trims the same way the caller does.
const HISTORY = 11;

/** Minimal in-memory stand-ins for the store, audit log and placement layer. */
function harness({ patterns, cooldownRounds = 1, maxBetsPerRun = 5, placeBetImpl, stakes = {} } = {}) {
    const patternState = {};
    const placed = [];
    const audits = [];
    const logs = [];

    const store = {
        forPattern(id) {
            patternState[id] ??= { betRoundIds: [] };
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

    const sums = [];
    const roundIds = [];
    let round = 0;
    /** Settle one more round with the given goal total and let the engine act. */
    const tick = (sum = 1, { fixture = { name: 'ARS - MUN' } } = {}) => {
        sums.push(sum);
        roundIds.push(`settled-${round}`);
        return engine.run({
            sums: sums.slice(-HISTORY),
            roundIds: roundIds.slice(-HISTORY),
            bettingRound: { id: `betting-${round++}` },
            resolveFixture: async () => fixture,
        });
    };
    /** Re-run the poll for the round already settled, changing nothing. */
    const repoll = () => engine.run({
        sums: sums.slice(-HISTORY),
        roundIds: roundIds.slice(-HISTORY),
        bettingRound: { id: `betting-${round - 1}` },
        resolveFixture: async () => ({ name: 'ARS - MUN' }),
    });

    return { engine, store, patternState, placed, audits, logs, tick, repoll };
}

test('a firing pattern places one bet, then stays quiet for the rounds it skips', async () => {
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
    assert.equal(h.placed.length, 2, 'betting resumes once the skipped rounds are through');
});

test('a wider pattern does not bet until it has counted a whole block itself', async () => {
    const h = harness({ patterns: [alwaysFires('p', 3)] });
    await h.tick();
    await h.tick();
    assert.equal(h.placed.length, 0, 'only 2 of 3 rounds counted');
    await h.tick();
    assert.equal(h.placed.length, 1);
});

test('a repeat poll within the same round never places a second bet', async () => {
    const h = harness({ patterns: [alwaysFires('p')], cooldownRounds: 0 });
    await h.tick();
    await h.repoll();
    await h.repoll();
    assert.equal(h.placed.length, 1);
});

test('a failed placement still consumes the block — it is unconfirmed, never retried', async () => {
    const h = harness({
        patterns: [alwaysFires('p')],
        cooldownRounds: 2,
        placeBetImpl: async () => { throw new Error('betslip did not clear'); },
    });
    await h.tick();
    assert.equal(h.audits.at(-1).success, false);
    assert.equal(h.patternState.p.cycle.skipRemaining, 2);
    await h.tick();
    assert.equal(h.audits.length, 1, 'no second attempt while the skipped rounds run out');
});

// --- fires the engine cannot act on -------------------------------------

test('a fire whose block is no longer the last settled round places nothing', async () => {
    // Two rounds land in one poll (a settlement backlog clearing). The block
    // completing on the older one called for a bet on a round that has already
    // kicked off, so it is reported and dropped, and the cycle moves on.
    const h = harness({ patterns: [alwaysFires('p', 2)], cooldownRounds: 0 });
    await h.engine.run({
        sums: [1, 1, 1, 1],
        roundIds: ['s1', 's2', 's3', 's4'],
        bettingRound: { id: 'b5' },
        resolveFixture: async () => ({ name: 'ARS - MUN' }),
    });
    // Cold start counts only s4, so nothing fires at all on this poll.
    assert.equal(h.placed.length, 0);

    const h2 = harness({ patterns: [alwaysFires('p', 2)], cooldownRounds: 0 });
    h2.patternState.p = { betRoundIds: [], cycle: { counted: 1, skipRemaining: 0, lastRoundId: 's1' } };
    await h2.engine.run({
        sums: [1, 1, 1],
        roundIds: ['s1', 's2', 's3'],
        bettingRound: { id: 'b4' },
        resolveFixture: async () => ({ name: 'ARS - MUN' }),
    });
    assert.equal(h2.placed.length, 0, 'the block ending at s2 is stale, and s3 only starts a new block');
    assert.ok(h2.logs.some((l) => l.includes('no longer the last settled round')));
});

test('a fire with no fixture on offer yet is held, not lost', async () => {
    const h = harness({ patterns: [alwaysFires('p')], cooldownRounds: 0 });
    await h.tick(1, { fixture: null });
    assert.equal(h.placed.length, 0);
    assert.ok(h.logs.some((l) => l.includes('no row-1 fixture yet')));

    // The cycle was rewound, so re-polling the SAME round reaches the block
    // again instead of it having been consumed by a fire that never happened.
    await h.repoll();
    assert.equal(h.placed.length, 1);
});

test('a fire held back by the per-run cap stays eligible', async () => {
    const h = harness({ patterns: [alwaysFires('a'), alwaysFires('b')], cooldownRounds: 0, maxBetsPerRun: 1 });
    await h.tick();
    assert.equal(h.placed.length, 1);
    assert.deepEqual(h.patternState.b.betRoundIds, [], 'the capped pattern never attempted');
    assert.ok(h.logs.some((l) => l.includes('[b]') && l.includes('cap reached')));
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
    // so a repeat poll stays quiet, and audited as having moved no money of
    // its own. Its cycle consumed the block just the same.
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
    await h.tick();
    await h.repoll();
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

test('the real trio/streak pair runs at 4 and 6 rounds, and coalesces where they meet', async () => {
    // Guards the reasoning in docs: the two low-scoring patterns match
    // together by construction, but each runs its own block cycle, so the
    // trio fires on rounds 3, 7, 11 and the streak on 5, 11 — contending
    // only where the rhythms come back into phase.
    const trio = alwaysFiresWanting('trio', 'Over 2.5', 3);
    const streak = alwaysFiresWanting('streak', 'Over 2.5', 5);
    const h = harness({ patterns: [streak, trio], cooldownRounds: 1 });

    const firedAt = [];
    for (let i = 1; i <= 11; i++) {
        const before = h.audits.length;
        await h.tick(1);
        for (const a of h.audits.slice(before)) firedAt.push(`${i}:${a.pattern}`);
    }
    assert.deepEqual(firedAt, ['3:trio', '5:streak', '7:trio', '11:streak', '11:trio']);

    // Round 11 is the collision, and it puts ONE Over 2.5 on, not two.
    assert.equal(h.placed.length, 4, 'rounds 3, 5, 7 and 11 — the round-11 duplicate is coalesced');
    const round11 = h.audits.slice(-2);
    assert.deepEqual(round11.map((a) => [a.pattern, a.placed !== false]), [['streak', true], ['trio', false]]);
});

test("one pattern's cycle does not mute another", async () => {
    const h = harness({ patterns: [alwaysFires('a'), neverFires('b')], cooldownRounds: 5 });
    await h.tick();                       // 'a' bets and starts skipping
    assert.equal(h.patternState.a.cycle.skipRemaining, 5);
    assert.equal(h.patternState.b.cycle.skipRemaining, 0, "'b' is untouched");
    assert.equal(h.patternState.b.cycle.counted, 0, "'b' missed its block and recounts from 0");
});
