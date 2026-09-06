// Life-cycle tests: the count -> judge -> reset (-> skip) rhythm that decides
// when a pattern is allowed to bet at all. Pure — no engine, no browser.
// Run with: npm run test:js

import test from 'node:test';
import assert from 'node:assert/strict';

import { advanceCycle, newCycle, rewindBeforeBlock } from '../../lib/pattern-cycle.js';

/**
 * Feed `sums` in one round at a time, the way the bot sees them, and report
 * which rounds the pattern fired on plus every event in order.
 */
function replay({ sums, windowSize, skipRounds, predicate = (s) => s <= 2 }) {
    const ids = sums.map((_, i) => `r${i + 1}`);
    let cycle = newCycle();
    const fires = [];
    const events = [];
    for (let n = 1; n <= sums.length; n++) {
        const seen = sums.slice(0, n);
        const res = advanceCycle({
            cycle, windowSize, skipRounds,
            roundIds: ids.slice(0, n),
            judge: (from, to) => seen.slice(from, to + 1).every(predicate),
        });
        cycle = res.cycle;
        for (const ev of res.events) {
            events.push(`${ev.roundId}:${ev.type}`);
            if (ev.type === 'fire') fires.push(ev.roundId);
        }
    }
    return { fires, events, cycle };
}

test('a block counts up to its window, fires, then skips before counting again', () => {
    const { events } = replay({ sums: [1, 1, 1, 1, 1], windowSize: 3, skipRounds: 1 });
    assert.deepEqual(events, [
        'r1:count',   // 1/3
        'r2:count',   // 2/3
        'r3:fire',    // 3/3 -> bet on r4, counter back to 0
        'r4:skip',    // the round being bet on is not counted
        'r5:count',   // 1/3 of the next block
    ]);
});

test('a miss restarts the count at the very next round, with no skip', () => {
    const { fires, events } = replay({ sums: [4, 1, 1, 1, 1, 1], windowSize: 3, skipRounds: 1 });
    assert.deepEqual(events.slice(0, 4), ['r1:count', 'r2:count', 'r3:miss', 'r4:count']);
    assert.deepEqual(fires, ['r6'], 'the block r4-r6 is judged immediately after the miss');
});

test('non-overlapping blocks cannot see a run that straddles a boundary', () => {
    // The accepted cost of judging whole blocks rather than sliding a window:
    // rounds 3-5 are a genuine low trio and are never judged as one, because
    // the counter reset at round 3 after the [1,4,1] block missed.
    assert.deepEqual(replay({ sums: [1, 4, 1, 1, 1], windowSize: 3, skipRounds: 1 }).fires, []);
});

test('trio and streak run at 4 and 6 rounds, and collide every 12', () => {
    const lows = Array(23).fill(1);
    const trio = replay({ sums: lows, windowSize: 3, skipRounds: 1 }).fires;
    const streak = replay({ sums: lows, windowSize: 5, skipRounds: 1 }).fires;

    assert.deepEqual(trio, ['r3', 'r7', 'r11', 'r15', 'r19', 'r23']);
    assert.deepEqual(streak, ['r5', 'r11', 'r17', 'r23']);
    // Independent cycles reduce contention; they do not remove it.
    assert.deepEqual(trio.filter((r) => streak.includes(r)), ['r11', 'r23']);
});

test('re-polling a round the cycle already consumed does nothing', () => {
    const roundIds = ['r1', 'r2', 'r3'];
    const args = { windowSize: 3, skipRounds: 1, roundIds, judge: () => true };
    const first = advanceCycle({ cycle: { counted: 2, skipRemaining: 0, lastRoundId: 'r2' }, ...args });
    assert.equal(first.events.filter((e) => e.type === 'fire').length, 1);

    const again = advanceCycle({ cycle: first.cycle, ...args });
    assert.deepEqual(again.events, [], 'no new rounds, so the block is not re-judged');
    assert.deepEqual(again.cycle, first.cycle);
});

test('a fresh cycle starts at the NEWEST settled round, not at whatever history exists', () => {
    // Otherwise the block boundaries would be an accident of how much history
    // the API happened to be serving when the process started — and a cold
    // start could fire off a run the bot never actually watched.
    const res = advanceCycle({
        cycle: newCycle(), windowSize: 3, skipRounds: 1,
        roundIds: ['r1', 'r2', 'r3', 'r4', 'r5'], judge: () => true,
    });
    assert.deepEqual(res.events.map((e) => `${e.roundId}:${e.type}`), ['r5:count']);
    assert.equal(res.restarted, true);
});

test('a cycle whose last round has fallen out of history restarts rather than guessing', () => {
    const res = advanceCycle({
        cycle: { counted: 2, skipRemaining: 0, lastRoundId: 'gone' },
        windowSize: 3, skipRounds: 1, roundIds: ['r8', 'r9'], judge: () => true,
    });
    assert.equal(res.restarted, true);
    assert.deepEqual(res.cycle, { counted: 1, skipRemaining: 0, lastRoundId: 'r9' });
});

test('a completed block whose earlier rounds are out of view is never judged', () => {
    // Judging a short block would make `every` vacuously true on a streak
    // pattern — a bet placed on no evidence at all.
    const res = advanceCycle({
        cycle: { counted: 2, skipRemaining: 0, lastRoundId: 'r1' },
        windowSize: 3, skipRounds: 1, roundIds: ['r1', 'r2'],
        judge: () => assert.fail('must not be judged'),
    });
    assert.deepEqual(res.events.map((e) => e.type), ['blind']);
    assert.equal(res.cycle.counted, 0);
});

test('several rounds arriving at once are consumed in order, not collapsed into one', () => {
    const res = advanceCycle({
        cycle: { counted: 1, skipRemaining: 0, lastRoundId: 'r1' },
        windowSize: 2, skipRounds: 1, roundIds: ['r1', 'r2', 'r3', 'r4', 'r5'], judge: () => true,
    });
    assert.deepEqual(res.events.map((e) => `${e.roundId}:${e.type}`),
        ['r2:fire', 'r3:skip', 'r4:count', 'r5:fire']);
});

test('rewindBeforeBlock puts the cycle back on the doorstep of the block it just judged', () => {
    const roundIds = ['r1', 'r2', 'r3'];
    const back = rewindBeforeBlock({ windowSize: 3, roundIds, index: 2 });
    assert.deepEqual(back, { counted: 2, skipRemaining: 0, lastRoundId: 'r2' });

    // Re-advancing from it reaches the same block again, so a fire the engine
    // could not act on yet is retried rather than lost.
    const res = advanceCycle({ cycle: back, windowSize: 3, skipRounds: 1, roundIds, judge: () => true });
    assert.deepEqual(res.events.map((e) => `${e.roundId}:${e.type}`), ['r3:fire']);
});

test('whether trio and streak ever collide is decided by their two periods', () => {
    // Their first fires land on rounds 3 and 5 — two apart — so they meet only
    // when the gcd of the two periods (window + skip) divides 2. Pinned here
    // because the useful lever is counter-intuitive: giving them EQUAL periods
    // separates them for good, since they can never come back into phase.
    const lows = Array(60).fill(1);
    const meet = (ts, ss) => {
        const t = replay({ sums: lows, windowSize: 3, skipRounds: ts }).fires;
        const s = replay({ sums: lows, windowSize: 5, skipRounds: ss }).fires;
        return t.filter((r) => s.includes(r));
    };
    assert.deepEqual(meet(1, 1), ['r11', 'r23', 'r35', 'r47', 'r59'], 'periods 4 and 6, gcd 2');
    assert.deepEqual(meet(0, 0), ['r15', 'r30', 'r45', 'r60'], 'periods 3 and 5, gcd 1');
    assert.deepEqual(meet(3, 1), [], 'periods 6 and 6 — equal, and permanently out of phase');
    assert.deepEqual(meet(1, 3), [], 'periods 4 and 8, gcd 4');
});
