// Presentation layer: the blocks the operator actually reads in the terminal.
//
// The point of these two blocks is a before/after pairing. Each round is
// printed twice:
//
//   NEXT   — the O/U prices the site is offering on the round now open for
//            betting. This is the "before": the market's own prediction.
//   RESULT — the same round once it has played out, with those exact captured
//            prices reprinted next to the score and the winning side ticked.
//            This is the "after".
//
// Normally the two are five minutes apart, but the gap is set by whenever the
// site actually publishes the result, which has been observed running to tens
// of minutes — so several NEXT blocks can go by before the matching RESULT
// lands. The matchday number in each header ("MD 07") is what pairs them up.
//
// The odds are reprinted on the RESULT block rather than left further up the
// scrollback so each block stands on its own and a single line of log is
// enough to see what was offered and what happened.
//
// Every function returns an ARRAY of lines, so the caller pushes each through
// the logger and the file keeps one timestamped record per line. Colour is
// emitted only for an interactive TTY, and the logger strips escape codes
// before anything reaches the log file.

import { formatCountdown, settleOverUnderOdds } from './betpawa/rounds.js';

const RULE_WIDTH = 74;

const useColor = Boolean(process.stdout.isTTY) && !process.env.NO_COLOR;
const sgr = (...codes) => (useColor ? `\x1b[${codes.join(';')}m` : '');
const RESET = sgr(0);

const C = {
    bold: sgr(1),
    dim: sgr(2),
    green: sgr(1, 32),
    yellow: sgr(1, 33),
    cyan: sgr(36),
    grey: sgr(90),
};

const paint = (color, text) => `${color}${text}${RESET}`;
const rule = (n) => paint(C.grey, '─'.repeat(Math.max(0, n)));

// Cells are space-padded so the tick column can never shift, which leaves
// trailing blanks on the last cell of a row. Harmless on screen, but they end
// up in the log file, so they are stripped from behind any closing reset.
// eslint-disable-next-line no-control-regex
const trimRowEnd = (line) => line.replace(/ +(\x1b\[0m)?$/, '$1');

/**
 * "── NEXT ── MD 34 · ARS - HUL ────────────── closes in 03:45 ──"
 * Lengths are computed from the plain text so the rule always lands on the
 * same column whether or not colour is on.
 */
function headerLine(label, labelColor, middle, right) {
    const head = `── ${label} ── ${middle} `;
    const tail = right ? `${right} ──` : '';
    const fill = Math.max(2, RULE_WIDTH - head.length - tail.length);
    return (
        rule(2) + ' ' + paint(labelColor, label) + ' ' + rule(2) + ' ' + paint(C.bold, middle) + ' ' +
        rule(fill) + (right ? ' ' + paint(C.cyan, right) + ' ' + rule(2) : '')
    );
}

/**
 * One "O/U 2.5   Over  1.58   Under  2.35" row per line.
 *
 * When `sum` is given the winning side is ticked and the losing side dimmed,
 * which is what makes an odds/result pair readable at a glance. When it is
 * undefined (the round has not been played yet) both sides print plain.
 */
function oddsRows(lines, sum) {
    const settled = sum === undefined ? lines : settleOverUnderOdds(lines, sum);
    return settled.map((line) => {
        const cell = (label, odds, side) => {
            // Fixed-width so the tick can never shift a column: every cell is
            // "Over  1.58  " or "Over  1.58 ✓".
            const text = `${label.padEnd(5)} ${odds.toFixed(2).padStart(5)} ${line.winner === side ? '✓' : ' '}`;
            if (line.winner === undefined) return text;
            return line.winner === side ? paint(C.green, text) : paint(C.dim, text);
        };
        return trimRowEnd(
            `   ${paint(C.grey, 'O/U')} ${String(line.total).padEnd(5)} ${cell('Over', line.over, 'Over')}   ${cell('Under', line.under, 'Under')}`
        );
    });
}

const note = (text) => `   ${paint(C.dim, text)}`;

/**
 * The "before" block: what the market is offering on the round that is open
 * for betting right now.
 *
 * `countdownRound` is the NEXT round, not this one — the site's own countdown
 * ticks down to the moment this round's betting closes (see getBettingRound).
 */
export function renderUpcomingOdds({ round, fixture, lines, countdownRound }) {
    return [
        '',
        headerLine('NEXT', C.yellow, `MD ${round.name} · ${fixture.name}`, `closes in ${formatCountdown(countdownRound)}`),
        ...(lines.length ? oddsRows(lines, undefined) : [note('(no O/U market offered on this round)')]),
    ];
}

/**
 * The "after" block: how that round finished, with the odds it had been
 * offering (`lines`, from the capture made while it was open — null if the
 * bot only started watching after it had kicked off).
 */
export function renderResult({ round, fixture, score, lines }) {
    return [
        '',
        headerLine('RESULT', C.cyan, `MD ${round.name} · ${fixture.name}`, `${score.sum} goal${score.sum === 1 ? '' : 's'}`),
        `   ${paint(C.grey, 'score')}     HT ${score.ht.home} - ${score.ht.away}     ${paint(C.bold, `FT ${score.ft.home} - ${score.ft.away}`)}     sum=${score.sum}`,
        ...(lines?.length
            ? oddsRows(lines, score.sum)
            : [note('(odds not captured — this round was already under way when the bot started watching)')]),
    ];
}

// Printed once at startup so the two block types are self-explanatory.
export function renderLegend() {
    return [
        paint(C.grey, 'Legend:  NEXT = O/U odds on the round now open for betting (the "before")'),
        paint(C.grey, '         RESULT = how that same round finished, with those odds reprinted (the "after")'),
        paint(C.grey, '         ✓ marks the side that won; the losing side is dimmed'),
    ];
}
