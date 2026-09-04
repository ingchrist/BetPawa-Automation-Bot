// Orchestration layer: given the settled-round history and the round that is
// currently open for betting, decides which patterns fire and places at most
// one bet.
//
// Each pattern carries its own cooldown and its own record of attempted
// rounds, so adding, removing or pausing one has no effect on the others.
// The engine itself contains no pattern-specific knowledge.

/**
 * Post-bet cooldown, counted in ROUNDS rather than wall-clock time: the rule
 * is "skip the next N rounds", and counting settled results themselves can't
 * drift with round length or expire on a timing knife edge the way a fixed
 * duration does.
 *
 * Pure: reports what the cooldown should become after observing the settled
 * round `roundId`; the caller writes the result back to state. `counted` is
 * false when this round was already counted (a repeat poll inside the same
 * round window, or a restart mid-cooldown), so the count advances exactly
 * once per round and the caller knows when to log.
 */
export function advanceCooldown(patternState, roundId) {
    const remaining = Number(patternState?.cooldownRoundsRemaining) || 0;
    if (remaining <= 0) return { paused: false, roundsRemaining: 0, counted: false };
    const counted = roundId !== patternState.cooldownLastCountedRoundId;
    return { paused: true, roundsRemaining: counted ? remaining - 1 : remaining, counted };
}

export function createPatternEngine({ patterns, config, store, auditLog, log, placeBet }) {
    // Process-local counters and display de-duplication. Deliberately not
    // persisted: the bet cap is documented as per-RUN, and the announcement
    // guards only exist to stop the same line repeating on every poll.
    let betsPlacedThisRun = 0;
    const announcedFireForRound = new Map(); // patternId -> bettingRoundId

    /**
     * @param {{ sums: number[], settledRoundId: string, bettingRound: object,
     *           fixture: object }} ctx  `sums` is oldest -> newest, long enough
     *           for the widest enabled pattern; `fixture` is the row-1 fixture
     *           of the betting round (fetched lazily by the caller only when
     *           something is about to fire).
     */
    async function run({ sums, settledRoundId, bettingRound, resolveFixture }) {
        let placedThisRound = false;

        for (const pattern of patterns) {
            const patternState = store.forPattern(pattern.id);
            const settings = config.forPattern(pattern.id);
            const tag = `[${pattern.id}]`;

            // Cooldown gate first: a paused pattern announces nothing and
            // places nothing, while results keep printing in the caller.
            const cooldown = advanceCooldown(patternState, settledRoundId);
            if (cooldown.paused) {
                if (cooldown.counted) {
                    patternState.cooldownRoundsRemaining = cooldown.roundsRemaining;
                    patternState.cooldownLastCountedRoundId = settledRoundId;
                    store.save();
                    log(cooldown.roundsRemaining > 0
                        ? `${tag} COOLDOWN: paused — ${cooldown.roundsRemaining} more round(s) to skip`
                        : `${tag} COOLDOWN: paused — resumes at the next result`);
                }
                continue;
            }
            if (patternState.cooldownLastCountedRoundId) {
                patternState.cooldownLastCountedRoundId = null;
                store.save();
                log(`${tag} COOLDOWN over — monitoring resumed`);
            }

            // Not enough settled history yet (fresh state, or right after a
            // season rollover). Expected on startup, so it stays silent.
            const window = sums.slice(-pattern.windowSize);
            if (window.length < pattern.windowSize) continue;

            if (!pattern.evaluate(window)) continue;

            const alreadyAnnounced = announcedFireForRound.get(pattern.id) === bettingRound.id;
            if (!alreadyAnnounced) {
                announcedFireForRound.set(pattern.id, bettingRound.id);
                log(`${tag} PATTERN FIRE: ${pattern.explain(window)} -> ${pattern.bet.selection} on the next round`);
            }

            if (store.hasAttempted(pattern.id, bettingRound.id)) continue; // handled already — stay quiet on repeat polls

            if (placedThisRound) {
                // Never build a multi-leg accumulator out of two patterns
                // firing on the same round. Not marked as attempted: the
                // round was never acted on for this pattern.
                log(`${tag} also fired for round ${bettingRound.id}, but a bet was already placed this round — skipping`);
                continue;
            }

            if (betsPlacedThisRun >= config.maxBetsPerRun) {
                // Deliberately not marked as attempted: never attempted, so it
                // stays eligible if the operator raises the cap or restarts.
                if (!alreadyAnnounced) log(`${tag} cap reached (${config.maxBetsPerRun}/run) — skipping bet placement`);
                continue;
            }

            const fixture = await resolveFixture();
            if (!fixture) {
                log(`${tag} no row-1 fixture yet for round ${bettingRound.id}, will retry next poll`);
                continue;
            }

            // Mark attempted BEFORE clicking, so a crash mid-click can never
            // result in a retry that double-bets the same round.
            store.markAttempted(pattern.id, bettingRound.id);
            store.save();
            placedThisRound = true;

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
                auditLog.append({ ...record, success: true, ...result });
                log(`${tag} BET PLACED: round ${bettingRound.id} ${fixture.name} ${pattern.bet.selection} stake=${settings.stakeFcfa} FCFA dryRun=${config.dryRun}`);
            } catch (err) {
                auditLog.append({ ...record, success: false, error: err.message });
                log(`${tag} BET FAILED: round ${bettingRound.id}: ${err.message}`);
            } finally {
                // Placement was ATTEMPTED, so this pattern goes quiet
                // regardless of outcome — a failed placement is unconfirmed
                // and must never be effectively retried. In `finally` so an
                // unexpected throw from the audit write can't skip it.
                patternState.cooldownRoundsRemaining = settings.cooldownRounds;
                // Seed with the round that fired: it is the one just
                // evaluated, not one of the rounds being skipped, so it must
                // not consume a slot in the count.
                patternState.cooldownLastCountedRoundId = settledRoundId;
                store.save();
                if (settings.cooldownRounds > 0) {
                    log(`${tag} COOLDOWN started: skipping the next ${settings.cooldownRounds} rounds (results keep printing)`);
                }
            }
        }
    }

    return { run, get betsPlacedThisRun() { return betsPlacedThisRun; } };
}
