// The pattern life cycle — the rule that decides WHEN a pattern is allowed to
// judge a block of rounds. Pure and I/O-free: it takes a pattern's persisted
// cycle state plus the settled rounds it has not seen yet, and reports what
// the cycle did.
//
// THE RULE (per pattern, independent of every other pattern)
//
//   counter starts at 0
//   each newly settled round advances it: 1, 2, ... up to windowSize
//   at windowSize the block just counted is judged, and either way the
//   counter goes back to 0:
//     - FIRES  -> bet on the next round, then SKIP `skipRounds` round(s)
//                 (the round being bet on) before counting starts again
//     - MISSES -> counting starts again immediately, at the very next round
//
// So a pattern of window N with skip S judges NON-OVERLAPPING blocks of N
// rounds, and after a fire its next block begins S rounds later. In an
// unbroken run of qualifying rounds it fires every N + S rounds: the 3-round
// trio at rounds 3, 7, 11, ... and the 5-round streak at 5, 11, 17, ...
//
// WHY BLOCKS, NOT A SLIDING WINDOW. The engine used to re-judge the last N
// sums on every round and suppress re-fires with a round-counted cooldown.
// That is equivalent to this cycle only when cooldown === N + S - 1, which
// the streak's cooldown of 3 was not: it re-armed two rounds early and reused
// rounds it had already bet off. Expressing the cycle directly makes the
// pattern's rhythm the thing that is configured, rather than something the
// reader has to derive from a cooldown number.
//
// THE COST, STATED PLAINLY. Non-overlapping blocks cannot see a qualifying
// run that straddles a block boundary: with sums 1, 4, 1, 1, 1 the trio
// judges [1,4,1], misses, and never looks at the real low trio in rounds
// 3-5. That is inherent to the rule, not a defect in this implementation.

/** Cycle state for a pattern that has never run. */
export function newCycle() {
    return { counted: 0, skipRemaining: 0, lastRoundId: null };
}

/**
 * Advance one pattern's cycle over every settled round it has not consumed.
 *
 * `roundIds` is the UNBROKEN run of settled rounds, oldest -> newest, that the
 * caller can also supply sums for; `judge(startIndex, endIndex)` is asked
 * whether the completed block spanning those indices fires.
 *
 * Returns the new cycle plus one event per round consumed, in order:
 *   { type: 'skip'  }        round not counted, it is being bet on
 *   { type: 'count' }        counter advanced, block not complete yet
 *   { type: 'miss'  }        block completed and did not fire
 *   { type: 'fire'  }        block completed and fired
 *   { type: 'blind' }        block completed but its earlier rounds have
 *                            dropped out of the visible history, so it could
 *                            not be judged at all
 * Every event carries `index`, `roundId` and `counted` (the counter's value
 * at that round, before any reset); block events also carry `blockStart`.
 *
 * `restarted` is true when the cycle could not be continued from where it left
 * off — a fresh state file, a permanent hole in the round history, or an
 * outage long enough that the last consumed round has scrolled away. In every
 * such case the cycle is zeroed and begins counting at the NEWEST settled
 * round: a life cycle is something the bot runs forward, and inferring one
 * from whatever history the API happens to still be serving would make the
 * block boundaries an accident of when the process started.
 */
export function advanceCycle({ cycle, windowSize, skipRounds, roundIds, judge }) {
    let { counted = 0, skipRemaining = 0, lastRoundId = null } = cycle ?? newCycle();

    const resumeAt = lastRoundId === null ? -1 : roundIds.indexOf(lastRoundId);
    const restarted = resumeAt < 0;
    if (restarted) {
        counted = 0;
        skipRemaining = 0;
    }
    // A restart consumes only the newest round; otherwise pick up straight
    // after the last round already consumed.
    let i = restarted ? roundIds.length - 1 : resumeAt + 1;

    const events = [];
    for (; i < roundIds.length; i++) {
        const roundId = roundIds[i];
        lastRoundId = roundId;

        if (skipRemaining > 0) {
            skipRemaining--;
            events.push({ type: 'skip', index: i, roundId, counted, skipRemaining });
            continue;
        }

        counted++;
        if (counted < windowSize) {
            events.push({ type: 'count', index: i, roundId, counted });
            continue;
        }

        const blockStart = i - windowSize + 1;
        if (blockStart < 0) {
            // Only reachable when the history shrank under a cycle that was
            // mid-block. Judging a short block would make `every` vacuously
            // true on a streak pattern, i.e. a bet placed on no evidence.
            counted = 0;
            events.push({ type: 'blind', index: i, roundId, counted: windowSize });
            continue;
        }

        const fired = judge(blockStart, i);
        counted = 0;
        if (fired) skipRemaining = skipRounds;
        events.push({ type: fired ? 'fire' : 'miss', index: i, roundId, counted: windowSize, blockStart });
    }

    return { cycle: { counted, skipRemaining, lastRoundId }, events, restarted };
}

/**
 * The cycle as it stood just BEFORE the completed block ending at `index` was
 * judged, so a fire the engine could not act on yet (no fixture on offer, bet
 * cap reached) can be un-consumed and re-reached on a later poll instead of
 * being silently swallowed by a cycle that has already moved on.
 */
export function rewindBeforeBlock({ windowSize, roundIds, index }) {
    return { counted: windowSize - 1, skipRemaining: 0, lastRoundId: roundIds[index - 1] ?? null };
}
