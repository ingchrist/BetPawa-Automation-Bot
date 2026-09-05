// The pattern registry — the single place to add a new betting pattern.
//
// To add one: create a module in this directory that default-exports an
// object matching the Pattern contract in ./streak.js (the createStreakPattern
// factory covers the "N consecutive rounds" shape), then import it and list it
// below. Nothing else in the codebase needs to change: config, state,
// cooldowns, the audit log and the placement flow are all keyed off the
// pattern's `id`.
//
// Order is the tie-break when several patterns fire on the same round. They
// are all placed, one after another, EXCEPT that a pattern asking for a
// selection an earlier one has already placed on that round is coalesced into
// it rather than staking the same bet twice (see lib/pattern-engine.js). So
// the earlier-listed pattern is the one whose stake is actually used.
//
// low-scoring-streak is listed before low-scoring-trio deliberately: the two
// bet the same Over 2.5, and the 5-round streak is the narrower, stronger
// signal, so it should own the placement on the rare rounds where both are
// off cooldown together.

import highScoringPair from './high-scoring-pair.js';
import lowScoringStreak from './low-scoring-streak.js';
import lowScoringTrio from './low-scoring-trio.js';

export const ALL_PATTERNS = Object.freeze([highScoringPair, lowScoringStreak, lowScoringTrio]);

/**
 * Patterns enabled by the current config, validated for id uniqueness.
 * @returns {Pattern[]}
 */
export function getEnabledPatterns(config) {
    const seen = new Set();
    for (const p of ALL_PATTERNS) {
        if (seen.has(p.id)) throw new Error(`duplicate pattern id "${p.id}" in the registry`);
        seen.add(p.id);
    }

    if (config.enabledPatternIds) {
        const unknown = config.enabledPatternIds.filter((id) => !seen.has(id));
        if (unknown.length) {
            throw new Error(`unknown pattern id(s): ${unknown.join(', ')} — known ids: ${[...seen].join(', ')}`);
        }
    }

    return ALL_PATTERNS.filter((p) => config.forPattern(p.id).enabled);
}

/** Largest window any enabled pattern needs — how far back history must reach. */
export function maxWindowSize(patterns) {
    return patterns.reduce((max, p) => Math.max(max, p.windowSize), 0);
}
