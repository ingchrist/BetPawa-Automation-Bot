// Offline life-cycle simulator: feeds a made-up sequence of round goal-totals
// through the REAL patterns, the REAL config and the REAL pattern engine, and
// prints what each pattern's counter does round by round.
//
// Touches no browser, no network, no state file and no betslip — the store is
// in memory and the placement layer is a stub. Safe to run at any time,
// including alongside a live bot.
//
// Usage:
//   node scripts/simulate-cycles.js                      the default scenario
//   node scripts/simulate-cycles.js --sums=1,2,1,5,1,1   your own sequence
//   node scripts/simulate-cycles.js --low=24             24 low rounds in a row
//   node scripts/simulate-cycles.js --cooldown=3         override every skip
//   node scripts/simulate-cycles.js --quiet              summary table only
//
// Config is read exactly as the bot reads it, so per-pattern env overrides
// apply here too — which is the only way to simulate UNEQUAL skips:
//   VIRTUAL_LOW_SCORING_TRIO_COOLDOWN_ROUNDS=3 node scripts/simulate-cycles.js --low=60

import { loadConfig } from '../lib/config.js';
import { getEnabledPatterns, maxWindowSize } from '../lib/patterns/index.js';
import { createPatternEngine } from '../lib/pattern-engine.js';
import { newCycle } from '../lib/pattern-cycle.js';

// A run of lows long enough for both low-scoring patterns to fire twice and
// collide once, a high round that breaks the streak, then lows again.
const DEFAULT_SUMS = [1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 1, 5, 1, 1, 1, 2];

// How much settled history the bot hands the engine: its widest pattern window
// plus LATE_RESULT_SLACK (see virtual-pattern-bot.js).
const LATE_RESULT_SLACK = 6;

const option = (name) => {
    const found = process.argv.slice(2).find((a) => a.startsWith(`--${name}=`));
    return found ? found.slice(name.length + 3) : undefined;
};
const flag = (name) => process.argv.slice(2).includes(`--${name}`);

const parseSums = () => {
    const explicit = option('sums');
    if (explicit) {
        const sums = explicit.split(',').map((s) => Number(s.trim()));
        if (sums.some((n) => !Number.isInteger(n) || n < 0)) {
            throw new Error(`--sums must be whole goal totals, e.g. --sums=1,2,1,5 (got "${explicit}")`);
        }
        return sums;
    }
    const low = option('low');
    if (low) return Array(Number(low)).fill(1);
    return DEFAULT_SUMS;
};

function main() {
    const quiet = flag('quiet');
    const sums = parseSums();
    const skipOverride = option('cooldown');

    // The bot's own config path, so .env, VIRTUAL_* vars and --patterns all apply.
    const baseConfig = loadConfig();
    const patterns = getEnabledPatterns(baseConfig);
    const historySize = maxWindowSize(patterns) + LATE_RESULT_SLACK;

    // The real config object, with only the skip swapped when asked for.
    const config = {
        ...baseConfig,
        maxBetsPerRun: Number.MAX_SAFE_INTEGER, // a per-RUN cap would distort a long simulation
        dryRun: true,
        forPattern: (id) => ({
            ...baseConfig.forPattern(id),
            cooldownRounds: skipOverride === undefined ? baseConfig.forPattern(id).cooldownRounds : Number(skipOverride),
        }),
    };

    // In-memory stand-in for the store: nothing here ever reaches disk.
    const state = {};
    const store = {
        forPattern(id) {
            state[id] ??= { betRoundIds: [], cycle: newCycle() };
            return state[id];
        },
        hasAttempted(id, roundId) { return this.forPattern(id).betRoundIds.includes(roundId); },
        markAttempted(id, roundId) { this.forPattern(id).betRoundIds.push(roundId); },
        save() {},
    };

    const placements = []; // { round, pattern, selection, coalescedInto }
    let currentRound = 0;
    const engine = createPatternEngine({
        patterns, config, store,
        auditLog: {
            append: (r) => placements.push({
                round: currentRound,
                pattern: r.pattern,
                selection: r.selection,
                coalescedInto: r.coalescedInto ?? null,
            }),
        },
        log: (m) => { if (!quiet) console.log(`   ${m}`); },
        placeBet: async () => ({ success: true }),
    });

    console.log(`skip after a fire: ${config.forPattern(patterns[0].id).cooldownRounds} round(s)   (--cooldown=N to change)`);
    console.log('patterns:');
    for (const p of patterns) {
        console.log(`  ${p.id.padEnd(20)} window ${p.windowSize}  ->  ${p.bet.selectionLabel}`);
    }
    console.log(`\nfeeding ${sums.length} settled rounds: ${sums.join(' ')}\n`);

    const seen = [];
    const roundIds = [];
    const run = async () => {
        for (let i = 0; i < sums.length; i++) {
            currentRound = i + 1;
            seen.push(sums[i]);
            roundIds.push(`r${i + 1}`);
            if (!quiet) console.log(`r${String(i + 1).padEnd(3)} settled, sum=${sums[i]}`);
            await engine.run({
                sums: seen.slice(-historySize),
                roundIds: roundIds.slice(-historySize),
                bettingRound: { id: `r${i + 2}` },
                resolveFixture: async () => ({ name: 'ARS - MUN' }),
            });
        }

        console.log('\nfires, by round:');
        const byRound = new Map();
        for (const p of placements) {
            if (!byRound.has(p.round)) byRound.set(p.round, []);
            byRound.get(p.round).push(p);
        }
        if (!byRound.size) console.log('  (none — no pattern completed a qualifying block)');
        for (const [round, entries] of [...byRound].sort((a, b) => a[0] - b[0])) {
            const bet = entries.find((e) => !e.coalescedInto);
            const also = entries.filter((e) => e.coalescedInto).map((e) => e.pattern);
            const tail = also.length
                ? `  <- ${also.join(', ')} fired too, coalesced into one bet at one stake`
                : '';
            console.log(`  r${String(round).padEnd(3)} ${bet.pattern.padEnd(20)} ${bet.selection} on r${round + 1}${tail}`);
        }

        console.log('\nper pattern:');
        for (const p of patterns) {
            const fired = placements.filter((x) => x.pattern === p.id).map((x) => x.round);
            if (!fired.length) {
                console.log(`  ${p.id.padEnd(20)} (never fired)`);
                continue;
            }
            const gaps = [...new Set(fired.slice(1).map((round, i) => round - fired[i]))];
            const rhythm = gaps.length ? `   every ${gaps.join(' / ')} rounds` : '';
            console.log(`  ${p.id.padEnd(20)} ${fired.map((r) => `r${r}`).join(' ')}${rhythm}`);
        }

        const collisions = [...byRound].filter(([, entries]) => entries.length > 1).map(([round]) => `r${round}`);
        console.log(`\ncollisions (two patterns firing on one round): ${collisions.length ? collisions.join(' ') : 'none'}`);

        console.log('\nno bet was placed and no state file was touched — this is a simulation.');
    };
    return run();
}

main().catch((err) => {
    console.error(err.message);
    process.exit(1);
});
