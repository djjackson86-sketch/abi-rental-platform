// Decode a PDF417 barcode from a grayscale PGM using the LOCALLY VENDORED ZXing
// bundle (static/vendor/zxing/zxing-library-0.21.3.min.js) — the exact bytes the
// browser loads. Proves the shipped engine actually decodes a real PDF417.
//
// The bundle is a UMD build: loaded here with CommonJS `require` (the same
// implementation the browser gets under `window.ZXing`), NOT inside a Node vm
// sandbox — ZXing searches the realm globals and cannot do that inside vm.
//
// Usage: node tests/js/pdf417_pgm_decode.mjs <file.pgm>
// Prints one JSON line: {"ok":true,"text":"..."} or {"ok":false,"error":"..."}

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';

const HERE = dirname(fileURLToPath(import.meta.url));
const BUNDLE_PATH = resolve(HERE, '..', '..', 'static', 'vendor', 'zxing', 'zxing-library-0.21.3.min.js');

function readPGM(path) {
  const buf = readFileSync(path);
  // Parse the P5 header: magic, width, height, maxval — then raw samples.
  let i = 0;
  const tokens = [];
  while (tokens.length < 4) {
    // skip whitespace
    while (i < buf.length && (buf[i] === 0x20 || buf[i] === 0x0a || buf[i] === 0x0d || buf[i] === 0x09)) { i += 1; }
    if (buf[i] === 0x23) { // '#' comment to end of line
      while (i < buf.length && buf[i] !== 0x0a) { i += 1; }
      continue;
    }
    let start = i;
    while (i < buf.length && buf[i] > 0x20) { i += 1; }
    tokens.push(buf.toString('ascii', start, i));
  }
  i += 1; // single whitespace after maxval
  const magic = tokens[0];
  if (magic !== 'P5') { throw new Error(`unsupported PGM magic ${magic}`); }
  const width = parseInt(tokens[1], 10);
  const height = parseInt(tokens[2], 10);
  const maxval = parseInt(tokens[3], 10);
  if (maxval > 255) { throw new Error('only 8-bit PGM is supported'); }
  const pixels = buf.subarray(i, i + width * height);
  if (pixels.length !== width * height) { throw new Error('PGM pixel data is short'); }
  // Grayscale bytes: RGBLuminanceSource treats a 1-byte-per-element array as luminance.
  return { width, height, pixels: new Uint8ClampedArray(pixels) };
}

function main() {
  const path = process.argv[2];
  if (!path) { console.error('usage: node pdf417_pgm_decode.mjs <file.pgm>'); process.exit(2); }

  const image = readPGM(path);
  const require = createRequire(import.meta.url);
  const ZX = require(BUNDLE_PATH);

  let outcome;
  try {
    if (!ZX || !ZX.PDF417Reader) { throw new Error('ZXing export missing'); }
    const source = new ZX.RGBLuminanceSource(image.pixels, image.width, image.height);
    const bitmap = new ZX.BinaryBitmap(new ZX.HybridBinarizer(source));
    const reader = new ZX.PDF417Reader();
    const hints = new Map([[ZX.DecodeHintType.TRY_HARDER, true]]);
    const result = reader.decode(bitmap, hints);
    outcome = { ok: true, text: result.getText() };
  } catch (err) {
    outcome = { ok: false, error: (err && err.name ? `${err.name}: ` : '') + (err && err.message ? err.message : String(err)) };
  }
  console.log(JSON.stringify(outcome));
  if (!outcome.ok) { process.exitCode = 1; }
}

main();
