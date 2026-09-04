// Durable state, persisted atomically so a crash mid-write can never leave a
// truncated file behind.
//
// Two things live here:
//   - roundSums: a rolling cache of settled rounds' full-time goal totals, so
//     a 5-round window costs one API fetch per new round instead of five per
//     poll. Authoritative: patterns are evaluated from it.
//   - patterns[id]: per-pattern bookkeeping (which rounds it has already
//     attempted, and its own cooldown). Namespacing by pattern id is what
//     lets patterns run side by side without one's cooldown muting another.

import fs from 'fs';
import path from 'path';

// Enough to serve the largest plausible pattern window many times over,
// while keeping the state file small and readable.
const ROUND_SUM_HISTORY = 40;
// Every round the bot has ever attempted would grow without bound; only the
// recent ones can still be re-offered by the site, so older ids are dead
// weight. Comfortably larger than any realistic backlog of open rounds.
const BET_ROUND_HISTORY = 50;

function defaultPatternState() {
    return {
        betRoundIds: [],
        // Post-bet cooldown. Both fields are persisted so a restart can
        // neither skip a pause that is still running nor double-count the
        // round it was already in the middle of.
        cooldownRoundsRemaining: 0,
        cooldownLastCountedRoundId: null,
    };
}

function defaultState() {
    return {
        version: 2,
        seasonId: null,
        roundSums: {},   // roundId -> { sum, startsAt }
        patterns: {},    // patternId -> defaultPatternState()
        updatedAt: null,
    };
}

/**
 * v1 (single hard-coded pattern) kept betRoundIds/cooldown* at the top level.
 * Move them under the pattern that owned them so an in-flight cooldown
 * survives the upgrade and the bot can't resume early and place the bet the
 * pause existed to prevent.
 *
 * v1's `lastPatternSums` is deliberately NOT carried over: it stored bare
 * sums with no timestamp, and round ids are not chronological across seasons
 * (see rounds.js), so those entries cannot be safely ordered. History
 * re-seeds itself from the API within one poll.
 */
export function migrateState(raw, legacyPatternId) {
    if (!raw || raw.version >= 2) return { ...defaultState(), ...raw };

    const state = { ...defaultState(), seasonId: raw.seasonId ?? null };
    state.patterns[legacyPatternId] = {
        ...defaultPatternState(),
        betRoundIds: Array.isArray(raw.betRoundIds) ? raw.betRoundIds.slice(-BET_ROUND_HISTORY) : [],
        cooldownRoundsRemaining: Number(raw.cooldownRoundsRemaining) || 0,
        cooldownLastCountedRoundId: raw.cooldownLastCountedRoundId ?? null,
    };

    // v0 used a wall-clock `cooldownUntil` stamp instead of a round count.
    // Convert whatever is left of it into whole rounds, rounding up.
    if (raw.cooldownUntil) {
        const until = Date.parse(raw.cooldownUntil);
        if (!Number.isNaN(until) && until > Date.now()) {
            const APPROX_ROUND_MS = 300500; // measured spacing between virtual rounds
            const rounds = Math.ceil((until - Date.now()) / APPROX_ROUND_MS);
            const p = state.patterns[legacyPatternId];
            p.cooldownRoundsRemaining = Math.max(p.cooldownRoundsRemaining, rounds);
        }
    }
    return state;
}

export function createStateStore({ filePath, legacyPatternId }) {
    let state;
    try {
        state = migrateState(JSON.parse(fs.readFileSync(filePath, 'utf8')), legacyPatternId);
    } catch {
        state = defaultState();
    }

    const save = () => {
        state.updatedAt = new Date().toISOString();
        const dir = path.dirname(filePath);
        if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
        const tmpPath = `${filePath}.tmp`;
        fs.writeFileSync(tmpPath, JSON.stringify(state, null, 2));
        fs.renameSync(tmpPath, filePath);
    };

    return {
        get raw() {
            return state;
        },
        save,

        get seasonId() {
            return state.seasonId;
        },
        set seasonId(id) {
            state.seasonId = id;
        },

        /** Per-pattern sub-state, created on first use. Mutate it, then save(). */
        forPattern(patternId) {
            if (!state.patterns[patternId]) state.patterns[patternId] = defaultPatternState();
            return state.patterns[patternId];
        },

        getRoundSum(roundId) {
            return state.roundSums[roundId]?.sum ?? null;
        },

        recordRoundSum(roundId, sum, startsAt) {
            state.roundSums[roundId] = { sum, startsAt };
            // Prune by trading-window start, never by id: ids are not
            // chronological across seasons.
            const ids = Object.keys(state.roundSums).sort(
                (a, b) => Date.parse(state.roundSums[a].startsAt) - Date.parse(state.roundSums[b].startsAt)
            );
            for (const id of ids.slice(0, Math.max(0, ids.length - ROUND_SUM_HISTORY))) {
                delete state.roundSums[id];
            }
        },

        hasAttempted(patternId, roundId) {
            return this.forPattern(patternId).betRoundIds.includes(roundId);
        },

        markAttempted(patternId, roundId) {
            const p = this.forPattern(patternId);
            p.betRoundIds.push(roundId);
            if (p.betRoundIds.length > BET_ROUND_HISTORY) {
                p.betRoundIds = p.betRoundIds.slice(-BET_ROUND_HISTORY);
            }
        },
    };
}
