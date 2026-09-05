// Logging infrastructure: timestamped file log + console mirror, with
// automatic pruning of old daily log files.
//
// Infrastructure only — it knows nothing about rounds, patterns or bets.

import fs from 'fs';
import path from 'path';

const DEFAULT_DIR = 'logs';
const DEFAULT_RETENTION_DAYS = 7;

// The console gets colour; the file must not. Escape codes in a log file
// break grep and show up as mojibake in any editor, so they are stripped on
// the way to disk rather than being kept out of the message in the first
// place.
// eslint-disable-next-line no-control-regex
const ANSI_PATTERN = /\x1b\[[0-9;]*m/g;

function cleanupOldLogs(dir, retentionDays) {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - retentionDays);
    for (const file of fs.readdirSync(dir)) {
        const filePath = path.join(dir, file);
        if (fs.statSync(filePath).mtime < cutoff) fs.unlinkSync(filePath);
    }
}

/**
 * @returns {{ log: (message: string) => void }}
 */
export function createLogger({ dir = DEFAULT_DIR, name = 'virtual-pattern-bot', retentionDays = DEFAULT_RETENTION_DAYS } = {}) {
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    cleanupOldLogs(dir, retentionDays);

    const today = new Date().toISOString().split('T')[0];
    const stream = fs.createWriteStream(path.join(dir, `${name}-${today}.log`), { flags: 'a' });

    return {
        log(message) {
            stream.write(`[${new Date().toISOString()}] ${message.replace(ANSI_PATTERN, '')}\n`);
            console.log(message);
        },
    };
}
