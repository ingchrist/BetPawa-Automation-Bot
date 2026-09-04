// Pattern 1 (original): two consecutive rounds that each finished with a
// total of 4 or more goals suggest the next round regresses to a lower
// score, so bet Under 3.5 (i.e. the next round totals 3 goals or fewer).

import { createStreakPattern } from './streak.js';

export default createStreakPattern({
    id: 'high-scoring-pair',
    name: '2 consecutive rounds with total goals >= 4 -> Under 3.5 on the next round',
    windowSize: 2,
    predicate: (sum) => sum >= 4,
    predicateLabel: 'with sum >= 4',
    bet: {
        marketTab: 'O/U',
        selectionLabel: 'Under 3.5',
        market: 'Over/Under Full Time',
        selection: 'Under 3.5',
    },
});
