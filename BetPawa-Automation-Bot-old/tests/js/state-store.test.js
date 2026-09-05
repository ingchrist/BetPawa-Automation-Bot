// State persistence and migration tests.
// Run with: npm run test:js

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { createStateStore, migrateState } from '../../lib/state-store.js';

const tmpFile = () => path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'vps-')), 'state.json');

test('v1 state moves its cooldown and bet history under the pattern that owned them', () => {
    const migrated = migrateState({
        seasonId: '138853',
        betRoundIds: ['1', '2'],
        lastPatternSums: { '1': 3 },
        cooldownRoundsRemaining: 2,
        cooldownLastCountedRoundId: '9',
    }, 'high-scoring-pair');

    assert.equal(migrated.version, 2);
    assert.equal(migrated.seasonId, '138853');
    assert.deepEqual(migrated.patterns['high-scoring-pair'], {
        betRoundIds: ['1', '2'],
        cooldownRoundsRemaining: 2,
        cooldownLastCountedRoundId: '9',
    });
    // v1 sums carried no timestamp and round ids are not chronological across
    // seasons, so they cannot be safely ordered — history re-seeds itself.
    assert.deepEqual(migrated.roundSums, {});
});

test('a v0 wall-clock cooldown is converted into whole rounds, never dropped', () => {
    const migrated = migrateState({ cooldownUntil: new Date(Date.now() + 11 * 60_000).toISOString() }, 'p');
    assert.ok(migrated.patterns.p.cooldownRoundsRemaining >= 2, 'an in-flight pause must survive the upgrade');
});

test('an expired v0 cooldown does not resurrect a pause', () => {
    const migrated = migrateState({ cooldownUntil: new Date(Date.now() - 60_000).toISOString() }, 'p');
    assert.equal(migrated.patterns.p.cooldownRoundsRemaining, 0);
});

test('migration is idempotent', () => {
    const once = migrateState({ betRoundIds: ['1'], cooldownRoundsRemaining: 1 }, 'p');
    assert.deepEqual(migrateState(structuredClone(once), 'p'), once);
});

test('a missing or corrupt state file starts clean instead of crashing', () => {
    const file = tmpFile();
    assert.equal(createStateStore({ filePath: file, legacyPatternId: 'p' }).raw.version, 2);
    fs.writeFileSync(file, 'not json{');
    assert.equal(createStateStore({ filePath: file, legacyPatternId: 'p' }).raw.version, 2);
});

test('state round-trips through disk', () => {
    const file = tmpFile();
    const store = createStateStore({ filePath: file, legacyPatternId: 'p' });
    store.seasonId = '42';
    store.markAttempted('p', 'r1');
    store.recordRoundSum('r1', 3, new Date(1000).toISOString());
    store.save();

    const reloaded = createStateStore({ filePath: file, legacyPatternId: 'p' });
    assert.equal(reloaded.seasonId, '42');
    assert.equal(reloaded.hasAttempted('p', 'r1'), true);
    assert.equal(reloaded.getRoundSum('r1'), 3);
    assert.equal(reloaded.getRoundSum('nope'), null);
});

test('round-sum history is pruned by trading start, never by id', () => {
    const store = createStateStore({ filePath: tmpFile(), legacyPatternId: 'p' });
    // Deliberately descending ids against ascending times — the season-rollover
    // case where sorting by id would evict the wrong (newest) entries.
    for (let i = 0; i < 60; i++) store.recordRoundSum(String(1000 - i), i, new Date(i * 1000).toISOString());

    const kept = Object.keys(store.raw.roundSums);
    assert.equal(kept.length, 40);
    assert.equal(store.getRoundSum('941'), 59, 'the newest entry survives');
    assert.equal(store.getRoundSum('1000'), null, 'the oldest entry is evicted');
});

test('attempted-round history stays bounded', () => {
    const store = createStateStore({ filePath: tmpFile(), legacyPatternId: 'p' });
    for (let i = 0; i < 80; i++) store.markAttempted('p', `r${i}`);
    const ids = store.forPattern('p').betRoundIds;
    assert.equal(ids.length, 50);
    assert.equal(ids.at(-1), 'r79', 'the most recent rounds are the ones kept');
});

test('each pattern gets its own independent slot', () => {
    const store = createStateStore({ filePath: tmpFile(), legacyPatternId: 'p' });
    store.markAttempted('a', 'r1');
    assert.equal(store.hasAttempted('a', 'r1'), true);
    assert.equal(store.hasAttempted('b', 'r1'), false);
});

test('captured odds survive a save/load round trip and prune by trading start', () => {
    const filePath = tmpFile();
    const lines = [{ total: 1.5, over: 1.15, under: 5.25 }];
    const store = createStateStore({ filePath, legacyPatternId: 'p' });

    assert.equal(store.getRoundOdds('r1'), null); // nothing captured yet
    store.recordRoundOdds('r1', lines, '2026-01-01T00:00:00Z');
    store.save();

    const reloaded = createStateStore({ filePath, legacyPatternId: 'p' });
    assert.deepEqual(reloaded.getRoundOdds('r1'), lines);

    // Odds captured for a round that started earlier must be the first to go,
    // regardless of id ordering — ids are not chronological across seasons.
    for (let i = 0; i < 60; i++) {
        reloaded.recordRoundOdds(`later-${i}`, lines, new Date(Date.UTC(2026, 1, 1) + i * 1000).toISOString());
    }
    assert.equal(reloaded.getRoundOdds('r1'), null);
    assert.deepEqual(reloaded.getRoundOdds('later-59'), lines);
});

test('a v2 state file written before odds capture existed still loads', () => {
    const filePath = tmpFile();
    fs.writeFileSync(filePath, JSON.stringify({ version: 2, seasonId: '1', roundSums: {}, patterns: {} }));
    const store = createStateStore({ filePath, legacyPatternId: 'p' });
    assert.equal(store.getRoundOdds('anything'), null);
    store.recordRoundOdds('r1', [{ total: 2.5, over: 1.58, under: 2.35 }], '2026-01-01T00:00:00Z');
    assert.equal(store.getRoundOdds('r1').length, 1);
});
