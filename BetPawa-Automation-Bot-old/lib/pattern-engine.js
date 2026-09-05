// Orchestration layer: given the settled-round history and the round that is
// currently open for betting, decides which patterns fire and places their
// bets.
//
// Each pattern carries its own cooldown and its own record of attempted
// rounds, so adding, removing or pausing one has no effect on the others.
// The engine itself contains no pattern-specific knowledge.
//
// SEVERAL PATTERNS CAN FIRE ON ONE ROUND. When they do, their bets are placed
// STRICTLY ONE AFTER ANOTHER — each placement is awaited to completion, and
// the placement layer starts every bet from a provably empty betslip, so two
// bets can never merge into a multi-leg accumulator (which is a different,
// far worse bet than the two singles the patterns actually asked for).
//
// The exception is a DUPLICATE: two patterns wanting the same selection on
// the same round is not two bets, it is one bet at double stake. That is
// never what a pattern asked for, so the second is coalesced into the first —
// no second placement, but the pattern is still recorded and still goes on
// cooldown, because the bet it wanted is on. This is not hypothetical:
// low-scoring-trio subsumes low-scoring-streak and both bet Over 2.5.

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

/** The identity of a bet, for spotting two patterns asking for the same one. */
const selectionKey = (bet) => `${bet.marketTab}|${bet.selectionLabel}`;

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
        // Selections acted on during THIS round -> which pattern acted, and
        // whether the placement was confirmed. Process-local: it exists only
        // to keep a second pattern from re-staking a bet that is already on
        // (or already in doubt).
        const claimedSelections = new Map(); // "O/U|Over 2.5" -> { patternId, confirmed }
        let placementsThisRound = 0;

        /**
         * Placement was attempted (or the bet is already on via another
         * pattern), so this pattern goes quiet for its cooldown. A failed
         * placement pauses just the same: it is unconfirmed, and must never be
         * effectively retried.
         */
        const startCooldown = (patternState, settings, tag) => {
            patternState.cooldownRoundsRemaining = settings.cooldownRounds;
            // Seed with the round that fired: it is the one just evaluated,
            // not one of the rounds being skipped, so it must not consume a
            // slot in the count.
            patternState.cooldownLastCountedRoundId = settledRoundId;
            store.save();
            if (settings.cooldownRounds > 0) {
                log(`${tag} COOLDOWN started: skipping the next ${settings.cooldownRounds} rounds (results keep printing)`);
            }
        };

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

            // Another pattern has already acted on this exact selection for
            // this round, so this one is coalesced into it: recorded, audited
            // as having moved no money of its own, and paused as if it had
            // placed. Two cases, both ending here:
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
                startCooldown(patternState, settings, tag);
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
            // Claimed BEFORE the await: if this placement throws, the claim
            // is what stops a later pattern from re-placing an unconfirmed
            // bet. Upgraded to confirmed only once the bookmaker has
            // acknowledged it.
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
                placementsThisRound++;
                claimedSelections.set(key, { patternId: pattern.id, confirmed: true });
                auditLog.append({ ...record, success: true, ...result });
                log(`${tag} BET PLACED: round ${bettingRound.id} ${fixture.name} ${pattern.bet.selection} stake=${settings.stakeFcfa} FCFA dryRun=${config.dryRun}`);
            } catch (err) {
                auditLog.append({ ...record, success: false, error: err.message });
                log(`${tag} BET FAILED: round ${bettingRound.id}: ${err.message}`);
            } finally {
                // In `finally` so an unexpected throw from the audit write
                // can't leave a pattern un-paused and free to fire again.
                startCooldown(patternState, settings, tag);
            }
        }
    }

    return { run, get betsPlacedThisRun() { return betsPlacedThisRun; } };
}
