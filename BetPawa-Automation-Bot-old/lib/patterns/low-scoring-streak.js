// Pattern 2: five consecutive rounds that each finished with a total of 2 or
// fewer goals suggest the next round breaks out high, so bet Over 2.5 (i.e.
// the next round totals 3 goals or more).
//
// Observed instance (logs/virtual-pattern-bot-2026-09-04.log, 18:06-18:31):
//   (ARS - AST) 1-1 sum=2
//   (ARS - LIV) 1-0 sum=1
//   (ARS - COV) 1-0 sum=1
//   (ARS - EVE) 1-1 sum=2
//   (AST - CHE) 0-1 sum=1   <- fifth consecutive round with sum <= 2
//   (ARS - MUN) 3-0 sum=3   <- next round, sum >= 3: Over 2.5 would have won

import { createStreakPattern } from './streak.js';

export default createStreakPattern({
    id: 'low-scoring-streak',
    name: '5 consecutive rounds with total goals <= 2 -> Over 2.5 on the next round',
    windowSize: 5,
    predicate: (sum) => sum <= 2,
    predicateLabel: 'with sum <= 2',
    bet: {
        marketTab: 'O/U',
        selectionLabel: 'Over 2.5',
        market: 'Over/Under Full Time',
        selection: 'Over 2.5',
    },
});
