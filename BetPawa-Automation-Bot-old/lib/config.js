// Configuration layer: turns CLI flags + environment into one plain, frozen
// config object. Nothing else in the codebase reads process.env or argv, so
// there is exactly one place to look for "how is this thing tuned".
//
// Global knobs (env, or .env):
//   CDP_ENDPOINT                 default http://127.0.0.1:9222
//   VIRTUAL_POLL_INTERVAL_MS     default 15000
//   VIRTUAL_MAX_BETS_PER_RUN     default 5   (across ALL patterns)
//   VIRTUAL_STAKE_FCFA           default 5   (any amount; per-pattern overridable)
//   VIRTUAL_COOLDOWN_ROUNDS      default 3   (per-pattern overridable)
//   VIRTUAL_PATTERNS             comma-separated pattern ids to enable (default: all)
//   VIRTUAL_LOG_DIR              default logs
//   VIRTUAL_STATE_PATH           default storage/virtual-pattern-state.json
//   VIRTUAL_AUDIT_LOG_PATH       default storage/logs/virtual-pattern-bets.jsonl
//
// Per-pattern overrides use the pattern id upper-snake-cased, e.g. for the
// pattern id "low-scoring-streak":
//   VIRTUAL_LOW_SCORING_STREAK_STAKE_FCFA
//   VIRTUAL_LOW_SCORING_STREAK_COOLDOWN_ROUNDS
//   VIRTUAL_LOW_SCORING_STREAK_ENABLED     ("false"/"0" to disable just this one)
//
// CLI flags (override env):
//   --dry-run           detect and log, never click Place Bet
//   --stake=25          global stake in FCFA
//   --patterns=a,b      enable only these pattern ids

import * as dotenv from 'dotenv';

const DEFAULTS = {
    cdpEndpoint: 'http://127.0.0.1:9222',
    logDir: 'logs',
    statePath: 'storage/virtual-pattern-state.json',
    auditLogPath: 'storage/logs/virtual-pattern-bets.jsonl',
    pollIntervalMs: 15000,
    maxBetsPerRun: 5,
    stakeFcfa: 5,
    cooldownRounds: 3,
};

/** "low-scoring-streak" -> "LOW_SCORING_STREAK" */
export function envKeyFor(patternId) {
    return patternId.toUpperCase().replace(/[^A-Z0-9]+/g, '_');
}

const parseFlag = (argv, name) => argv.includes(`--${name}`);

const parseOption = (argv, name) => {
    const prefix = `--${name}=`;
    const found = argv.find((a) => a.startsWith(prefix));
    return found ? found.slice(prefix.length) : undefined;
};

const isFalsey = (value) => /^(false|0|no|off)$/i.test(String(value).trim());

/**
 * A positive number, or the fallback when unset/blank/unparseable — a typo in
 * a tuning env var must never silently become NaN and disable a safety gate.
 */
function positiveNumber(value, fallback) {
    if (value === undefined || value === null || String(value).trim() === '') return fallback;
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** Same, but 0 is a legitimate value (a cooldown of 0 rounds means "no pause"). */
function nonNegativeNumber(value, fallback) {
    if (value === undefined || value === null || String(value).trim() === '') return fallback;
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? n : fallback;
}

export function loadConfig({ argv = process.argv.slice(2), env = process.env } = {}) {
    // process.env wins over .env, matching the previous behaviour.
    const merged = { ...(dotenv.config().parsed || {}), ...env };

    const stakeFcfa = positiveNumber(parseOption(argv, 'stake') ?? merged.VIRTUAL_STAKE_FCFA, DEFAULTS.stakeFcfa);
    const cooldownRounds = nonNegativeNumber(merged.VIRTUAL_COOLDOWN_ROUNDS, DEFAULTS.cooldownRounds);

    const patternsOption = parseOption(argv, 'patterns') ?? merged.VIRTUAL_PATTERNS;
    const enabledPatternIds = patternsOption
        ? patternsOption.split(',').map((s) => s.trim()).filter(Boolean)
        : null; // null = "every registered pattern"

    return Object.freeze({
        cdpEndpoint: merged.CDP_ENDPOINT || DEFAULTS.cdpEndpoint,
        logDir: merged.VIRTUAL_LOG_DIR || DEFAULTS.logDir,
        statePath: merged.VIRTUAL_STATE_PATH || DEFAULTS.statePath,
        auditLogPath: merged.VIRTUAL_AUDIT_LOG_PATH || DEFAULTS.auditLogPath,
        pollIntervalMs: positiveNumber(merged.VIRTUAL_POLL_INTERVAL_MS, DEFAULTS.pollIntervalMs),
        maxBetsPerRun: positiveNumber(merged.VIRTUAL_MAX_BETS_PER_RUN, DEFAULTS.maxBetsPerRun),
        dryRun: parseFlag(argv, 'dry-run'),
        stakeFcfa,
        cooldownRounds,
        enabledPatternIds,

        /**
         * Resolved settings for one pattern: its own override if present,
         * otherwise the global default.
         * @returns {{ enabled: boolean, stakeFcfa: number, cooldownRounds: number }}
         */
        forPattern(patternId) {
            const key = envKeyFor(patternId);
            const explicitlyDisabled = merged[`VIRTUAL_${key}_ENABLED`] !== undefined
                && isFalsey(merged[`VIRTUAL_${key}_ENABLED`]);
            return {
                enabled: !explicitlyDisabled
                    && (enabledPatternIds === null || enabledPatternIds.includes(patternId)),
                stakeFcfa: positiveNumber(merged[`VIRTUAL_${key}_STAKE_FCFA`], stakeFcfa),
                cooldownRounds: nonNegativeNumber(merged[`VIRTUAL_${key}_COOLDOWN_ROUNDS`], cooldownRounds),
            };
        },
    });
}
