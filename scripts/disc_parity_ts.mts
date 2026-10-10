/**
 * Reference-fixture generator for the ABI licence-disc parser port (programme phase 1 / A1).
 *
 * Runs the REAL, already-shipping TrailerPro parser
 *   /mnt/d/Claude/trailer-rental-app/src/lib/saDiscParser.ts
 * over every payload in tests/fixtures/disc/*.txt and writes the results to
 * tests/fixtures/disc/expected_ts.json. The Python port
 * (app/services/vehicle_disk.py) is asserted against that file, so the port is
 * verified against the reference implementation instead of being approximated.
 *
 * Usage:  node --import tsx scripts/disc_parity_ts.mts        (tsx must be resolvable)
 *         /path/to/node_modules/.bin/tsx scripts/disc_parity_ts.mts
 *
 * Read-only: this script never writes outside the ABI repo.
 */
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
// @ts-expect-error - path is outside this repo; tsx resolves the real TS source
import { parseSADisc } from '/mnt/d/Claude/trailer-rental-app/src/lib/saDiscParser.ts';

const FIXTURE_DIR = join(process.cwd(), 'tests', 'fixtures', 'disc');
const OUT = join(FIXTURE_DIR, 'expected_ts.json');

// The reference parser logs verbosely via console.log — silence it so only our JSON lands on stdout.
const realLog = console.log;
console.log = () => {};

const out: Record<string, unknown> = {};
for (const name of readdirSync(FIXTURE_DIR).filter((f) => f.endsWith('.txt')).sort()) {
  const payload = readFileSync(join(FIXTURE_DIR, name), 'utf8');
  out[name.replace(/\.txt$/, '')] = parseSADisc(payload);
}

console.log = realLog;
writeFileSync(OUT, JSON.stringify(out, null, 2) + '\n', 'utf8');
realLog(`wrote ${OUT} (${Object.keys(out).length} fixtures): ${Object.keys(out).join(', ')}`);
