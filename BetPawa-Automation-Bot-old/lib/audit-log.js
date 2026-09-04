// Append-only JSONL audit trail of every placement ATTEMPT (success or not).
// Deliberately separate from the human-readable log: this one is the record
// used to reconcile the bot against the bookmaker's own bet history.

import fs from 'fs';
import path from 'path';

export function createAuditLog(filePath) {
    return {
        /** @param {Record<string, unknown>} record */
        append(record) {
            const dir = path.dirname(filePath);
            if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
            fs.appendFileSync(filePath, JSON.stringify({ timestamp: new Date().toISOString(), ...record }) + '\n');
        },
    };
}
