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
// row-1 fixture. They fire together whenever both are off cooldown, which the
// engine resolves by coalescing the duplicate rather than staking twice on one
// selection (see the same-round handling in lib/pattern-engine.js). In
// practice the per-pattern cooldown usually staggers them: this one fires at
// round 3 and is still paused when the streak pattern reaches round 5.

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
