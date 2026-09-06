// Orchestration layer: given the settled-round history and the round that is
// currently open for betting, runs each pattern's life cycle and places the
// bets that come out of it.
//
// Each pattern owns its own cycle (lib/pattern-cycle.js) and its own record of
// attempted rounds, so adding, removing or pausing one has no effect on the
// others. The engine itself contains no pattern-specific knowledge.
//
// THE CYCLE IS THE ONCE-PER-ROUND GATE. A pattern's cycle advances only when a
// settled round it has not consumed appears, and that state is persisted — so
// re-polling the same round, or restarting the process, cannot make a pattern
// judge the same block twice, and no separate "already announced" or cooldown
// bookkeeping is needed to suppress it.
//
// INDEPENDENT CYCLES STILL COLLIDE. Running on their own rhythms does not keep
// the patterns apart: over an unbroken low run the trio fires on rounds
// 3, 7, 11, ... and the streak on 5, 11, 17, ..., so they contend every 12
// rounds — and a cold start puts both at round N of a fresh cycle together.
// When they do collide their bets are placed STRICTLY ONE AFTER ANOTHER — each
// placement is awaited to completion, and the placement layer starts every bet
// from a provably empty betslip, so two bets can never merge into a multi-leg
// accumulator (a different, far worse bet than the two singles the patterns
// actually asked for).
//
// The exception is a DUPLICATE: two patterns wanting the same selection on the
// same round is not two bets, it is one bet at double stake. That is never what
// a pattern asked for, so the second is coalesced into the first — no second
// placement, but it is still recorded and audited, because the bet it wanted is
// on. This is not hypothetical: low-scoring-trio subsumes low-scoring-streak
// and both bet Over 2.5.

import { advanceCycle, rewindBeforeBlock } from './pattern-cycle.js';

/** The identity of a bet, for spotting two patterns asking for the same one. */
const selectionKey = (bet) => `${bet.marketTab}|${bet.selectionLabel}`;

export function createPatternEngine({ patterns, config, store, auditLog, log, placeBet }) {
    // Process-local: the bet cap is documented as per-RUN.
    let betsPlacedThisRun = 0;

    /**
     * @param {{ sums: number[], roundIds: string[], bettingRound: object,
     *           resolveFixture: () => Promise<object|null> }} ctx
     *   `sums` and `roundIds` are the same unbroken run of settled rounds,
     *   oldest -> newest and index-aligned; `resolveFixture` yields the row-1
     *   fixture of the betting round, fetched lazily only when something is
     *   about to fire.
     */
    async function run({ sums, roundIds, bettingRound, resolveFixture }) {
        // Selections acted on during THIS round -> which pattern acted, and
        // whether the placement was confirmed. Process-local: it exists only
        // to keep a second pattern from re-staking a bet that is already on
        // (or already in doubt).
        const claimedSelections = new Map(); // "O/U|Over 2.5" -> { patternId, confirmed }
        let placementsThisRound = 0;
        const newestIndex = roundIds.length - 1;

        /**
         * Everything that happens once a block has fired on the newest settled
         * round. Returns true when the bet could not be attempted yet and the
         * cycle should be rewound so the next poll reaches this block again.
         */
        async function handleFire({ pattern, settings, tag, window, bettingRound, resolveFixture, claimedSelections, onPlaced, placementsThisRound }) {
            // Already handled — a rewind that raced a placement, say. Let the
            // cycle move on rather than rewinding onto it forever.
            if (store.hasAttempted(pattern.id, bettingRound.id)) return false;

            // Another pattern has already acted on this exact selection for
            // this round, so this one is coalesced into it: recorded, and
            // audited as having moved no money of its own. Two cases, both
            // ending here:
            //   - the earlier placement was CONFIRMED: re-placing would double
            //     the stake on one bet, not add a second one.
            //   - it FAILED, i.e. is unconfirmed: the bet may well be on at
            //     the bookmaker, so placing the same selection again is a
            //     retry in disguise. Fail closed, exactly as the placement
            //     layer does with its own submit click.
            const key = selectionKey(pattern.bet);
            const claim = claimedSelections.get(key);
            if (claim) {
                store.markAttempted(pattern.id, bettingRound.id);
                store.save();
                auditLog.append({
                    pattern: pattern.id,
                    roundId: bettingRound.id,
                    fixture: (await resolveFixture())?.name ?? null,
                    market: pattern.bet.market,
                    selection: pattern.bet.selection,
                    stake: 0,                           // no money moved for this pattern
                    requestedStake: settings.stakeFcfa, // what it would have staked on its own
                    sums: window,
                    success: claim.confirmed,
                    placed: false,
                    coalescedInto: claim.patternId,
                });
                log(claim.confirmed
                    ? `${tag} also fired for round ${bettingRound.id} wanting the same ${pattern.bet.selection} — already placed by [${claim.patternId}], not staking it twice`
                    : `${tag} also fired for round ${bettingRound.id} wanting the same ${pattern.bet.selection} — [${claim.patternId}]'s attempt is unconfirmed, not re-placing it`);
                return false;
            }

            if (betsPlacedThisRun >= config.maxBetsPerRun) {
                // Never attempted, so the cycle is rewound and this block stays
                // eligible if the operator raises the cap or restarts.
                log(`${tag} cap reached (${config.maxBetsPerRun}/run) — holding this fire for a later poll`);
                return true;
            }

            const fixture = await resolveFixture();
            if (!fixture) {
                log(`${tag} no row-1 fixture yet for round ${bettingRound.id}, will retry next poll`);
                return true;
            }

            // Mark attempted BEFORE clicking, so a crash mid-click can never
            // result in a retry that double-bets the same round.
            store.markAttempted(pattern.id, bettingRound.id);
            store.save();
            // Claimed BEFORE the await: if this placement throws, the claim is
            // what stops a later pattern from re-placing an unconfirmed bet.
            // Upgraded to confirmed only once the bookmaker has acknowledged it.
            claimedSelections.set(key, { patternId: pattern.id, confirmed: false });
            if (placementsThisRound > 0) {
                log(`${tag} a bet has already gone on this round — placing this one after it, as a separate single (never one multi-leg slip)`);
            }

            const record = {
                pattern: pattern.id,
                roundId: bettingRound.id,
                fixture: fixture.name,
                market: pattern.bet.market,
                selection: pattern.bet.selection,
                stake: settings.stakeFcfa,
                sums: window,
            };

            try {
                const result = await placeBet({
                    fixtureName: fixture.name,
                    marketTab: pattern.bet.marketTab,
                    selectionLabel: pattern.bet.selectionLabel,
                    stakeFcfa: settings.stakeFcfa,
                });
                betsPlacedThisRun++;
                onPlaced();
                claimedSelections.set(key, { patternId: pattern.id, confirmed: true });
                auditLog.append({ ...record, success: true, ...result });
                log(`${tag} BET PLACED: round ${bettingRound.id} ${fixture.name} ${pattern.bet.selection} stake=${settings.stakeFcfa} FCFA dryRun=${config.dryRun}`);
            } catch (err) {
                // A failed placement is UNCONFIRMED, never retried: the bet may
                // be on at the bookmaker. The cycle moves on exactly as if it
                // had succeeded.
                auditLog.append({ ...record, success: false, error: err.message });
                log(`${tag} BET FAILED: round ${bettingRound.id}: ${err.message}`);
            }
            return false;
        }

        for (const pattern of patterns) {
            const patternState = store.forPattern(pattern.id);
            const settings = config.forPattern(pattern.id);
            const tag = `[${pattern.id}]`;

            const { cycle, events, restarted } = advanceCycle({
                cycle: patternState.cycle,
                windowSize: pattern.windowSize,
                skipRounds: settings.cooldownRounds,
                roundIds,
                judge: (from, to) => pattern.evaluate(sums.slice(from, to + 1)),
            });

            if (!events.length) continue; // no round this pattern has not already consumed — stay silent

            // A cold start is the normal case and says nothing interesting; a
            // cycle that was running and could not be continued does.
            if (restarted && patternState.cycle?.lastRoundId) {
                log(`${tag} CYCLE RESTARTED: the round history no longer reaches round ${patternState.cycle.lastRoundId}, so the block in progress cannot be trusted — counting starts again from 0`);
            }

            // The advanced cycle is not written back until every event has
            // been handled: a fire the engine cannot act on YET has to leave
            // the cycle standing just before that block, so the next poll
            // reaches it again instead of the bet being lost to a cycle that
            // has already moved past it.
            let pending = cycle;

            for (const ev of events) {
                const at = `${ev.counted}/${pattern.windowSize}`;
                if (ev.type === 'skip') {
                    log(`${tag} SKIP round ${ev.roundId} — the round it bet on, so it is not counted${ev.skipRemaining > 0 ? ` (${ev.skipRemaining} more to skip)` : `; the next block starts at the next round`}`);
                } else if (ev.type === 'count') {
                    log(`${tag} cycle ${at} — round ${ev.roundId} counted (sum ${sums[ev.index]})`);
                } else if (ev.type === 'blind') {
                    log(`${tag} cycle ${at} — block complete but its earlier rounds are no longer in view, so it cannot be judged; counting restarts at 0`);
                } else if (ev.type === 'miss') {
                    log(`${tag} cycle ${at} — no fire (sums: ${sums.slice(ev.blockStart, ev.index + 1).join(', ')}); counting restarts at 0`);
                } else if (ev.type === 'fire') {
                    const window = sums.slice(ev.blockStart, ev.index + 1);
                    log(`${tag} PATTERN FIRE: ${pattern.explain(window)} -> ${pattern.bet.selection} on the next round`);

                    // Only a block ending at the NEWEST settled round can be
                    // acted on: `bettingRound` is the round after that one, so
                    // a fire further back is judging rounds that no longer
                    // immediately precede the bet — a different strategy from
                    // the one the pattern describes. The cycle still advances
                    // past it, exactly as it would have live.
                    if (ev.index !== newestIndex) {
                        log(`${tag} that block ended at round ${ev.roundId}, which is no longer the last settled round — the bet it called for is gone, no placement`);
                        continue;
                    }

                    const deferred = await handleFire({
                        pattern, settings, tag, window,
                        bettingRound, resolveFixture, claimedSelections,
                        onPlaced: () => { placementsThisRound++; },
                        placementsThisRound,
                    });
                    if (deferred) {
                        pending = rewindBeforeBlock({ windowSize: pattern.windowSize, roundIds, index: ev.index });
                    }
                }
            }

            patternState.cycle = pending;
            store.save();
        }
    }

    return { run, get betsPlacedThisRun() { return betsPlacedThisRun; } };
}
