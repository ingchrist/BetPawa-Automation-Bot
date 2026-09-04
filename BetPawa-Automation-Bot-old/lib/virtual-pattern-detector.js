// Pure detection layer for the virtual-football pattern bot.
// All functions are either pure or take `page` only as an opaque fetch executor,
// so the pattern math can be tested without a real browser.

export const LEAGUE_ID = '7794'; // English League

export const BETPAWA_HEADERS = {
    'X-Pawa-Brand': 'betpawa-cameroon',
    'deviceType': 'web',
    'Accept': 'application/json',
    'X-Pawa-Language': 'en',
};

export const SEASONS_ACTUAL_URL = 'https://www.betpawa.cm/api/sportsbook/virtual/v2/seasons/list/actual';
export const EVENTS_BY_ROUND_URL = (roundId) =>
    `https://www.betpawa.cm/api/sportsbook/virtual/v3/events/list/by-round/${roundId}`;

const FULL_TIME_SLUG = 'FULL_TIME_EXCLUDING_OVERTIME';
const FIRST_HALF_SLUG = 'FIRST_HALF';

// Confirmed live: a round's FULL_TIME_EXCLUDING_OVERTIME value can appear
// with a provisional score that gets corrected a few seconds later (e.g.
// observed 1 -> 2 fifteen seconds apart) as the round's result finishes
// posting — so its mere presence is NOT sufficient proof of finality.
// Results were observed to settle within ~4-32s of a round's tradingTime.end;
// this buffer adds real margin on top of that measured range.
export const FINALITY_BUFFER_MS = 60000;

export function isRoundSettled(round, nowMs = Date.now()) {
    return Date.parse(round.tradingTime.end) + FINALITY_BUFFER_MS <= nowMs;
}

// Must run inside the real logged-in page: the API validates an
// X-Device-Fingerprint header tied to the actual browser, so a separate
// Node-side HTTP client with copied cookies is not reliable here.
//
// Confirmed live: these endpoints sit behind Cloudflare with
// `Cache-Control: public, max-age=30` but `Vary: accept-encoding` only — NOT
// `Accept`. Since we deliberately send `Accept: application/json` (the real
// frontend sends `application/x-protobuf`), a plain request would share the
// same edge cache slot as real visitors' requests to the identical URL,
// letting our JSON response get served to them (or vice versa) for up to
// 30s — this was observed to genuinely break the live site's rendering.
// A unique cache-busting query param puts every request of ours in its own
// cache key that no real visitor's canonical-URL request will ever match,
// fully isolating our traffic from the shared cache.
function withCacheBuster(url) {
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}_cb=${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function fetchJsonInPage(page, url) {
    return page.evaluate(
        async ({ url, headers }) => {
            const r = await fetch(url, { headers, cache: 'no-store' });
            if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
            return r.json();
        },
        { url: withCacheBuster(url), headers: BETPAWA_HEADERS }
    );
}

export async function fetchSeasonsActual(page) {
    const data = await fetchJsonInPage(page, SEASONS_ACTUAL_URL);
    return data.items || [];
}

export async function fetchRoundEvents(page, roundId) {
    const data = await fetchJsonInPage(page, EVENTS_BY_ROUND_URL(roundId));
    return data.responses || [];
}

// Round `id` is NOT chronological across seasons — confirmed live that a
// later season can have lower ids than the currently-active one (e.g.
// season N+1's ids were numerically *below* season N's while N was still
// live), so sorting by id can put a future season's rounds ahead of the
// live one. Sort by the actual trading window instead.
export function sortRoundsAsc(rounds) {
    return [...rounds].sort((a, b) => Date.parse(a.tradingTime.start) - Date.parse(b.tradingTime.start));
}

// Returns {round, index} for the first round whose trading window hasn't
// started yet, or null if none found (e.g. stale/empty round list).
export function findNextUpcomingRound(roundsAsc, nowMs = Date.now()) {
    const index = roundsAsc.findIndex((r) => Date.parse(r.tradingTime.start) > nowMs);
    if (index === -1) return null;
    return { round: roundsAsc[index], index };
}

// "Row 1" = the first fixture as actually DISPLAYED on the site for the
// target league. The by-round endpoint mixes ~66 fixtures across 7 leagues
// in an unrelated internal order — confirmed live that raw JSON order does
// NOT match on-screen order at all (e.g. site showed "ARS - BHA" as row 1
// while raw order's first English-League entry was "TOT - BRE", a totally
// different fixture). The real page sorts each league's fixtures
// alphabetically by fixture name before rendering, so replicate that here
// rather than trusting API array order.
export function getRowOneFixture(responses) {
    const leagueFixtures = responses
        .filter((e) => e.competition && e.competition.id === LEAGUE_ID)
        .sort((a, b) => a.name.localeCompare(b.name));
    return leagueFixtures[0] || null;
}

function findPeriodResult(fixture, participantType, slug) {
    const pr = (fixture.results?.participantPeriodResults || []).find(
        (p) => p.participant?.type === participantType
    );
    if (!pr) return null;
    const period = (pr.periodResults || []).find((p) => p.period?.slug === slug);
    return period ? Number(period.result) : null;
}

function findFullTimeResult(fixture, participantType) {
    return findPeriodResult(fixture, participantType, FULL_TIME_SLUG);
}

// A round is only "finalized" once both HOME and AWAY have a full-time
// score recorded. `results.display.currentPeriod`/`minute` can look
// stale/mid-match even after the round is fully final — never use it here.
export function isFixtureFinalized(fixture) {
    return findFullTimeResult(fixture, 'HOME') !== null && findFullTimeResult(fixture, 'AWAY') !== null;
}

export function getFullTimeScore(fixture) {
    const home = findFullTimeResult(fixture, 'HOME');
    const away = findFullTimeResult(fixture, 'AWAY');
    if (home === null || away === null) return null;
    return { home, away, sum: home + away };
}

// Half-time + full-time score together, for display purposes — matches what
// the frontend itself shows: "(HT_H - HT_A) FT_H - FT_A". Half-time is
// cosmetic only (defaults to 0-0 if missing) since the pattern only ever
// judges the full-time sum.
export function getScoreDisplay(fixture) {
    const ftHome = findFullTimeResult(fixture, 'HOME');
    const ftAway = findFullTimeResult(fixture, 'AWAY');
    if (ftHome === null || ftAway === null) return null;
    const htHome = findPeriodResult(fixture, 'HOME', FIRST_HALF_SLUG) ?? 0;
    const htAway = findPeriodResult(fixture, 'AWAY', FIRST_HALF_SLUG) ?? 0;
    return { ht: { home: htHome, away: htAway }, ft: { home: ftHome, away: ftAway }, sum: ftHome + ftAway };
}

// e.g. "(AST - BHA)   (2 - 0) 3 - 2" — mirrors the site's own fixture row.
export function formatFixtureLine(fixture, score) {
    return `(${fixture.name})   (${score.ht.home} - ${score.ht.away}) ${score.ft.home} - ${score.ft.away}`;
}

// e.g. "03:42" — mirrors the site's own "Starts in: 03:42" countdown.
export function formatCountdown(round, nowMs = Date.now()) {
    const ms = Date.parse(round.tradingTime.start) - nowMs;
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

export function evaluatePattern({ sum1, sum2 }) {
    return sum1 >= 4 && sum2 >= 4;
}

// Post-bet cooldown, counted in ROUNDS rather than wall-clock time: the rule
// is "skip the next N rounds", and counting the settled results themselves
// can't drift with round length or expire on a timing knife edge the way a
// fixed duration does.
//
// Pure: reports what the cooldown should become after observing the settled
// round `roundId`; the caller writes the result back to state. `counted` is
// false when this round was already counted (a repeat poll inside the same
// round window, or a restart mid-cooldown), so the count advances exactly
// once per round and the caller knows when to log.
export function advanceCooldown(state, roundId) {
    const remaining = Number(state?.cooldownRoundsRemaining) || 0;
    if (remaining <= 0) return { paused: false, roundsRemaining: 0, counted: false };
    const counted = roundId !== state.cooldownLastCountedRoundId;
    return { paused: true, roundsRemaining: counted ? remaining - 1 : remaining, counted };
}

// One-time migration from the earlier wall-clock cooldown (an ISO
// `cooldownUntil` stamp) to the round-counted one above, so a restart during
// a still-running pause can't resume early and place the bet the pause
// existed to prevent. Mutates `state`, returns the rounds still to skip.
// Delete once no state file in the wild carries `cooldownUntil` any more.
export function migrateLegacyCooldown(state, roundMs, nowMs = Date.now()) {
    if (!state?.cooldownUntil) return state?.cooldownRoundsRemaining || 0;
    const until = Date.parse(state.cooldownUntil);
    delete state.cooldownUntil;
    if (!Number.isNaN(until) && until > nowMs) {
        const rounds = Math.ceil((until - nowMs) / roundMs);
        state.cooldownRoundsRemaining = Math.max(state.cooldownRoundsRemaining || 0, rounds);
    }
    return state.cooldownRoundsRemaining || 0;
}
