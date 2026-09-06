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
// Matching together is not firing together, though. On the default cooldown of
// 3 both run on a 4-round rhythm, and this one always reaches its window two
// rounds ahead of the streak, so they lock into anti-phase and alternate
// indefinitely (trio at low 3, streak at low 5, trio at low 7, ...). They only
// genuinely collide when something breaks that lock: a cooldown of 0, unequal
// per-pattern cooldowns, or — the realistic one — the bot starting up when the
// last 5 settled rounds are already all <= 2, so both fire on the first poll.
// The engine coalesces that duplicate rather than staking twice on one
// selection; see the same-round handling in lib/pattern-engine.js.

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
