// Durable state, persisted atomically so a crash mid-write can never leave a
// truncated file behind.
//
// Two things live here:
//   - roundSums: a rolling cache of settled rounds' full-time goal totals, so
//     a 5-round window costs one API fetch per new round instead of five per
//     poll. Authoritative: patterns are evaluated from it.
//   - roundOdds: the Over/Under prices a round was offering while it was open
//     for betting. This is a CAPTURE, not a cache: the site drops a round's
//     markets the instant it kicks off (see getOverUnderOdds), so if these
//     are not written down while the round is open they are gone for good and
//     the result can never be shown next to the odds that preceded it.
//   - patterns[id]: per-pattern bookkeeping (which rounds it has already
//     attempted, and its own life cycle — see lib/pattern-cycle.js).
//     Namespacing by pattern id is what lets patterns run side by side on
//     their own rhythms without one's pauses muting another.

import fs from 'fs';
import path from 'path';

import { newCycle } from './pattern-cycle.js';

// Enough to serve the largest plausible pattern window many times over,
// while keeping the state file small and readable.
const ROUND_SUM_HISTORY = 40;
// Every round the bot has ever attempted would grow without bound; only the
// recent ones can still be re-offered by the site, so older ids are dead
// weight. Comfortably larger than any realistic backlog of open rounds.
const BET_ROUND_HISTORY = 50;
// Odds are captured one round BEFORE the matching result prints, and are
// worth keeping around afterwards as the raw material for "what did the
// market think, and what actually happened". Same order as the sum history.
const ROUND_ODDS_HISTORY = 40;

function defaultPatternState() {
    return {
        betRoundIds: [],
        // The pattern's life cycle. Persisted in full so a restart resumes the
        // block it was part-way through instead of starting a new one — which
        // would re-align every block boundary to whenever the process happened
        // to be restarted. See lib/pattern-cycle.js.
        cycle: newCycle(),
    };
}

function defaultState() {
    return {
        version: 3,
        seasonId: null,
        roundSums: {},   // roundId -> { sum, startsAt }
        roundOdds: {},   // roundId -> { lines: [{ total, over, under }], startsAt }
        patterns: {},    // patternId -> defaultPatternState()
        updatedAt: null,
    };
}

/**
 * v2 -> v3: a per-pattern round-counted COOLDOWN became an explicit life cycle
 * (lib/pattern-cycle.js). Whatever is left of a cooldown carries over as rounds
 * still to be SKIPPED, and the round it had counted up to becomes the round the
 * cycle resumes after — so an upgrade mid-pause can neither resume early and
 * place the bet the pause existed to prevent, nor re-consume a round twice. The
 * block itself always restarts from 0, which is the conservative direction: a
 * v2 sliding window could fire sooner than a v3 block ever will.
 *
 * v1 (single hard-coded pattern) kept betRoundIds/cooldown* at the top level;
 * move them under the pattern that owned them first, then apply the above.
 *
 * v1's `lastPatternSums` is deliberately NOT carried over: it stored bare sums
 * with no timestamp, and round ids are not chronological across seasons (see
 * rounds.js), so those entries cannot be safely ordered. History re-seeds itself
 * from the API within one poll.
 */
export function migrateState(raw, legacyPatternId) {
    if (!raw) return defaultState();
    if (raw.version >= 3) return { ...defaultState(), ...raw };

    // Normalise v0/v1's top-level bookkeeping into the v2 per-pattern shape,
    // so there is only one conversion to v3 below.
    let patterns = raw.patterns ?? {};
    if (!(raw.version >= 2)) {
        let cooldownRoundsRemaining = Number(raw.cooldownRoundsRemaining) || 0;
        // v0 used a wall-clock `cooldownUntil` stamp instead of a round count.
        // Convert whatever is left of it into whole rounds, rounding up.
        if (raw.cooldownUntil) {
            const until = Date.parse(raw.cooldownUntil);
            if (!Number.isNaN(until) && until > Date.now()) {
                const APPROX_ROUND_MS = 300500; // measured spacing between virtual rounds
                cooldownRoundsRemaining = Math.max(cooldownRoundsRemaining, Math.ceil((until - Date.now()) / APPROX_ROUND_MS));
            }
        }
        patterns = {
            [legacyPatternId]: {
                betRoundIds: Array.isArray(raw.betRoundIds) ? raw.betRoundIds.slice(-BET_ROUND_HISTORY) : [],
                cooldownRoundsRemaining,
                cooldownLastCountedRoundId: raw.cooldownLastCountedRoundId ?? null,
            },
        };
    }

    const state = { ...defaultState(), seasonId: raw.seasonId ?? null };
    if (raw.version >= 2) {
        state.roundSums = raw.roundSums ?? {};
        state.roundOdds = raw.roundOdds ?? {};
    }
    for (const [id, p] of Object.entries(patterns)) {
        state.patterns[id] = {
            ...defaultPatternState(),
            betRoundIds: Array.isArray(p?.betRoundIds) ? p.betRoundIds.slice(-BET_ROUND_HISTORY) : [],
            cycle: p?.cycle ?? {
                counted: 0,
                skipRemaining: Number(p?.cooldownRoundsRemaining) || 0,
                lastRoundId: p?.cooldownLastCountedRoundId ?? null,
            },
        };
    }
    return state;
}

// Prune by trading-window start, never by id: ids are not chronological
// across seasons (see rounds.js).
function pruneByStart(map, keep) {
    const ids = Object.keys(map).sort((a, b) => Date.parse(map[a].startsAt) - Date.parse(map[b].startsAt));
    for (const id of ids.slice(0, Math.max(0, ids.length - keep))) delete map[id];
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
            pruneByStart(state.roundSums, ROUND_SUM_HISTORY);
        },

        /** The captured O/U lines for a round, or null if none were captured. */
        getRoundOdds(roundId) {
            return state.roundOdds[roundId]?.lines ?? null;
        },

        recordRoundOdds(roundId, lines, startsAt) {
            state.roundOdds[roundId] = { lines, startsAt };
            pruneByStart(state.roundOdds, ROUND_ODDS_HISTORY);
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
