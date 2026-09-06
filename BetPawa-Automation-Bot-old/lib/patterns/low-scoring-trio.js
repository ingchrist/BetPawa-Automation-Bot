// Pattern 3: three consecutive rounds that each finished with a total of 2 or
// fewer goals suggest the next round breaks out high, so bet Over 2.5 (i.e.
// the next round totals 3 goals or more).
//
// Observed instance (logs/virtual-pattern-bot-2026-09-04.log):
//   (ARS - COV) 1-0 sum=1
//   (ARS - EVE) 1-1 sum=2
//   (AST - CHE) 0-1 sum=1   <- third consecutive round with sum <= 2
//   (ARS - MUN) 3-0 sum=3   <- next round, sum >= 3: Over 2.5 would have won
//
// RELATIONSHIP TO low-scoring-streak: this is the same rule with a shorter
// window, so it STRICTLY SUBSUMES it — every 5-round low streak contains a
// 3-round low tail, and both bet the identical Over 2.5 on the identical
// row-1 fixture. Whenever the streak MATCHES, this one matches too.
//
// Matching together is not firing together, though. Each pattern runs its own
// life cycle of non-overlapping blocks (lib/pattern-cycle.js), so on the
// default skip of 1 this one fires every 3 + 1 = 4 rounds and the streak every
// 5 + 1 = 6. Over an unbroken low run starting at round 1 that puts the trio on
// rounds 3, 7, 11, 15, 19, 23 and the streak on 5, 11, 17, 23: they contend on
// round 11 and every 12 rounds after it, where the two rhythms come back into
// phase. Independent cycles reduce collisions; they do not remove them.
//
// What independent cycles DO remove is the cold-start collision: a cycle that
// cannot be resumed starts counting at the newest settled round, so both
// patterns begin from the same origin and neither can arrive at a full window
// on the first poll off the back of history it never watched.
//
// When they do contend, the engine coalesces the duplicate rather than staking
// twice on one selection; see the same-round handling in lib/pattern-engine.js.

import { createStreakPattern } from './streak.js';

export default createStreakPattern({
    id: 'low-scoring-trio',
    name: '3 consecutive rounds with total goals <= 2 -> Over 2.5 on the next round',
    windowSize: 3,
    predicate: (sum) => sum <= 2,
    predicateLabel: 'with sum <= 2',
    bet: {
        marketTab: 'O/U',
        selectionLabel: 'Over 2.5',
        market: 'Over/Under Full Time',
        selection: 'Over 2.5',
    },
});
