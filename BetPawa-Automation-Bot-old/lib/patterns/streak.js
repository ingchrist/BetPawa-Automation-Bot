// Factory for the "N consecutive rounds whose total goals satisfy a
// predicate" family of patterns — the shape both currently-known patterns
// take. A pattern that is NOT a streak (alternating results, a specific
// scoreline, ...) does not have to use this: it just has to export an object
// matching the Pattern contract below, and be registered in ./index.js.
//
// @typedef {object} BetSpec
// @property {string} marketTab       on-screen market tab, e.g. 'O/U'
// @property {string} selectionLabel  on-screen odd label, e.g. 'Over 2.5'
// @property {string} market          human/audit name of the market
// @property {string} selection       human/audit name of the selection
//
// @typedef {object} Pattern
// @property {string}   id          stable key — used in state, audit log and env var names
// @property {string}   name        one-line human description, printed at startup
// @property {number}   windowSize  how many consecutive settled rounds it inspects
// @property {BetSpec}  bet         what to place when it fires
// @property {(sums:number[]) => boolean} evaluate  sums oldest -> newest, length === windowSize
// @property {(sums:number[]) => string}  explain   why it fired, for the log line

/**
 * @param {{ id: string, name: string, windowSize: number, predicate: (sum:number)=>boolean,
 *           predicateLabel: string, bet: BetSpec }} spec
 * @returns {Pattern}
 */
export function createStreakPattern({ id, name, windowSize, predicate, predicateLabel, bet }) {
    if (!Number.isInteger(windowSize) || windowSize < 1) {
        throw new Error(`pattern "${id}": windowSize must be a positive integer, got ${windowSize}`);
    }
    return Object.freeze({
        id,
        name,
        windowSize,
        bet: Object.freeze({ ...bet }),
        // The caller is responsible for passing a full, settled window; the
        // length check is a cheap guard against a partial one slipping
        // through and making `every` vacuously true.
        evaluate: (sums) => sums.length === windowSize && sums.every(predicate),
        explain: (sums) => `${windowSize} consecutive rounds ${predicateLabel} (sums: ${sums.join(', ')})`,
    });
}
