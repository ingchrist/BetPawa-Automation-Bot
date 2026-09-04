// Pure domain logic for virtual-football rounds: ordering, settlement,
// score extraction and display formatting. No I/O, no browser, no patterns —
// every function here is deterministic and unit-testable as-is.

import { LEAGUE_ID } from './api.js';

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

// roundsAsc[nextIndex] is the first round whose OWN trading window hasn't
// started yet — but the real site is NOT open for new bets on that round.
// Confirmed live (matched the site's on-screen row-1 fixture against exact
// round timestamps): the site is still showing/taking bets on
// roundsAsc[nextIndex-1] — the round that started but hasn't ended — right up
// until its tradingTime.end (which equals roundsAsc[nextIndex].tradingTime
// .start, so the "Starts in" countdown the site displays next to that fixture
// is really counting down to THIS round's own close, not to when betting
// opens). So the round to actually act on is nextIndex-1.
export function getBettingRound(roundsAsc, nextIndex) {
    return roundsAsc[nextIndex - 1] || null;
}

// The consecutive settled rounds immediately preceding the betting round,
// oldest -> newest, at most `maxSize` of them. Confirmed live that a round's
// result only posts within seconds of its own window closing, i.e. right as
// it would stop being the betting round, so the betting round itself can
// never be observed finalized while still open — the newest usable result is
// always nextIndex-2.
//
// Returns the LONGEST fully-settled suffix, which may be shorter than
// `maxSize` (early in a round list, or just after a season rollover) and is
// empty while the newest candidate round is still running. Callers evaluate
// each pattern only if the window is at least as long as that pattern needs,
// so a wide pattern simply waits for history to build up while narrower ones
// keep working.
export function getSettledWindow(roundsAsc, nextIndex, maxSize, nowMs = Date.now()) {
    const end = nextIndex - 1; // exclusive: nextIndex-2 is the newest settled round
    const start = Math.max(0, end - maxSize);
    if (end <= 0) return [];
    const candidate = roundsAsc.slice(start, end);
    let firstSettled = candidate.length;
    for (let i = candidate.length - 1; i >= 0; i--) {
        if (!isRoundSettled(candidate[i], nowMs)) break;
        firstSettled = i;
    }
    return candidate.slice(firstSettled);
}

// "Row 1" = the first fixture as actually DISPLAYED on the site for the
// target league. The by-round endpoint mixes ~66 fixtures across 7 leagues
// in an unrelated internal order — confirmed live that raw JSON order does
// NOT match on-screen order at all (e.g. site showed "ARS - BHA" as row 1
// while raw order's first English-League entry was "TOT - BRE", a totally
// different fixture). The real page sorts each league's fixtures
// alphabetically by fixture name before rendering, so replicate that here
// rather than trusting API array order.
export function getRowOneFixture(responses, leagueId = LEAGUE_ID) {
    const leagueFixtures = responses
        .filter((e) => e.competition && e.competition.id === leagueId)
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

const findFullTimeResult = (fixture, participantType) => findPeriodResult(fixture, participantType, FULL_TIME_SLUG);

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
// cosmetic only (defaults to 0-0 if missing) since patterns only ever judge
// the full-time sum.
export function getScoreDisplay(fixture) {
    const ft = getFullTimeScore(fixture);
    if (!ft) return null;
    const htHome = findPeriodResult(fixture, 'HOME', FIRST_HALF_SLUG) ?? 0;
    const htAway = findPeriodResult(fixture, 'AWAY', FIRST_HALF_SLUG) ?? 0;
    return { ht: { home: htHome, away: htAway }, ft: { home: ft.home, away: ft.away }, sum: ft.sum };
}

// e.g. "(AST - BHA)   (2 - 0) 3 - 2" — mirrors the site's own fixture row.
export function formatFixtureLine(fixture, score) {
    return `(${fixture.name})   (${score.ht.home} - ${score.ht.away}) ${score.ft.home} - ${score.ft.away}`;
}

// e.g. "03:42" — mirrors the site's own "Starts in: 03:42" countdown.
export function formatCountdown(round, nowMs = Date.now()) {
    const totalSec = Math.max(0, Math.floor((Date.parse(round.tradingTime.start) - nowMs) / 1000));
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
