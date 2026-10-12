// Geometry regression for the licence-disk scanner: a disc barcode that fits INSIDE
// the alignment guide must be readable, and the whole-frame path must not be the only
// one the scanner has.
//
// Measured from the client's own scan video (a circular RSA windshield disc behind wet
// glass), the PDF417 occupies roughly 18% of the preview width and its row structure is
// not resolvable. That case is reproduced here from a SYNTHETIC fixture
// (tests/fixtures/pdf417_synth_disc_175x55.pgm — fake values, no real disc data)
// pasted 1:1 into a portrait 1080x1920 frame, and decoded with the SAME vendored ZXing
// bundle the browser loads. The module's own bounds are parsed out of
// static/js/scan-camera.js, so changing them means re-checking here.
//
// This is a geometry/engine test. It is NOT a camera test and proves nothing about
// physical iPhone capture — see the honest limitations in the release notes.
//
// Usage: node tests/js/scan_disc_roi_decode.mjs
// Exits non-zero on the first failed expectation.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { createRequire } from 'node:module';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, '..', '..');
const MODULE = readFileSync(resolve(ROOT, 'static', 'js', 'scan-camera.js'), 'utf8');
const BUNDLE = resolve(ROOT, 'static', 'vendor', 'zxing', 'zxing-library-0.21.3.min.js');
const FIXTURE = resolve(HERE, '..', 'fixtures', 'pdf417_synth_disc_175x55.pgm');
const PAYLOAD = '%ZA99999%148%MOTORVOERTUIGLISENSIE%1%ABC1234%XYZ9876%CN123456%MOTOR CAR%TOYOTA%HILUX%WHITE%SYNTHVIN0000000001%SYNTHENG000000001%20270430%';

let failures = 0;
let checks = 0;
function ok(condition, label) {
  checks += 1;
  if (condition) { console.log(`  ok   ${label}`); } else { failures += 1; console.log(`  FAIL ${label}`); }
}

function moduleConst(name) {
  const match = MODULE.match(new RegExp('var\\s+' + name + '\\s*=\\s*(\\d+)'));
  if (!match) { throw new Error(`static/js/scan-camera.js no longer defines ${name}`); }
  return Number(match[1]);
}

function readPGM(path) {
  const buf = readFileSync(path);
  let i = 0;
  const tokens = [];
  while (tokens.length < 4) {
    while (i < buf.length && [0x20, 0x0a, 0x0d, 0x09].includes(buf[i])) { i += 1; }
    if (buf[i] === 0x23) { while (i < buf.length && buf[i] !== 0x0a) { i += 1; } continue; }
    const start = i;
    while (i < buf.length && buf[i] > 0x20) { i += 1; }
    tokens.push(buf.toString('ascii', start, i));
  }
  i += 1;
  const width = parseInt(tokens[1], 10), height = parseInt(tokens[2], 10);
  return { width, height, pixels: new Uint8ClampedArray(buf.subarray(i, i + width * height)) };
}

// Paste the barcode 1:1 into a flat portrait frame (what the camera would hand over).
function composeFrame(barcode, W, H, bx, by) {
  const pixels = new Uint8ClampedArray(W * H).fill(205);
  for (let y = 0; y < barcode.height; y += 1) {
    for (let x = 0; x < barcode.width; x += 1) {
      pixels[(by + y) * W + (bx + x)] = barcode.pixels[y * barcode.width + x];
    }
  }
  return pixels;
}

// Stands in for canvas drawImage(video, sx, sy, sw, sh, 0, 0, dw, dh).
function cropScale(pixels, W, H, sx, sy, sw, sh, dw, dh) {
  const out = new Uint8ClampedArray(dw * dh);
  for (let y = 0; y < dh; y += 1) {
    const fy = Math.min(H - 1, Math.max(0, sy + ((y + 0.5) * sh) / dh - 0.5));
    const y0 = Math.floor(fy), y1 = Math.min(H - 1, y0 + 1), wy = fy - y0;
    for (let x = 0; x < dw; x += 1) {
      const fx = Math.min(W - 1, Math.max(0, sx + ((x + 0.5) * sw) / dw - 0.5));
      const x0 = Math.floor(fx), x1 = Math.min(W - 1, x0 + 1), wx = fx - x0;
      const a = pixels[y0 * W + x0], b = pixels[y0 * W + x1];
      const c = pixels[y1 * W + x0], d = pixels[y1 * W + x1];
      out[y * dw + x] = Math.round(a * (1 - wx) * (1 - wy) + b * wx * (1 - wy) + c * (1 - wx) * wy + d * wx * wy);
    }
  }
  return { width: dw, height: dh, pixels: out };
}

const require = createRequire(import.meta.url);
const ZX = require(BUNDLE);

function decode(image) {
  const source = new ZX.RGBLuminanceSource(image.pixels, image.width, image.height);
  const bitmap = new ZX.BinaryBitmap(new ZX.HybridBinarizer(source));
  try {
    return new ZX.PDF417Reader().decode(bitmap, new Map([[ZX.DecodeHintType.TRY_HARDER, true]])).getText();
  } catch (err) {
    return null;      // NotFoundException / ChecksumException == "no barcode in this frame"
  }
}

console.log('licence-disk decode geometry');

const W = 1080, H = 1920;                 // a portrait iPhone rear stream
const barcode = readPGM(FIXTURE);
const frame = composeFrame(barcode, W, H, 340, 1250);

// The scanner must decode the guide region first, then the whole frame.
ok(/var region = guideRegion\(\);\s*var text = tryFrame\(region\);\s*if \(!text && region\) \{ text = tryFrame\(null\); \}/.test(MODULE),
  'the scanner decodes the guide region FIRST and only then the whole frame');
ok(/guide\.getBoundingClientRect\(\)/.test(MODULE), 'the guide rectangle is measured, not guessed');
ok(/if \(scale > budget\) \{ scale = budget; \}/.test(MODULE), 'a crop larger than the budget is still bounded');

// (a) the whole-frame path, exactly as the module bounds it
const wholeScale = Math.min(1, moduleConst('MAX_DECODE_WIDTH') / W, moduleConst('MAX_DECODE_HEIGHT') / H);
const whole = cropScale(frame, W, H, 0, 0, W, H, Math.round(W * wholeScale), Math.round(H * wholeScale));
const wholeText = decode(whole);
console.log(`  whole-frame decode ${whole.width}x${whole.height} -> ${wholeText ? 'DECODED' : 'no barcode'}`);
ok(wholeText === null, 'the whole-frame path cannot read a guide-sized disc barcode (the reported failure)');
ok(whole.width * whole.height < W * H, 'and that path is a downscale of the portrait frame');

// (b) the guide crop, at native camera pixels, up to ROI_UPSCALE and within the pixel budget
const pad = 10;
const guide = { x: 340 - pad, y: 1250 - pad, w: barcode.width + pad * 2, h: barcode.height + pad * 2 };
const maxPixels = moduleConst('ROI_MAX_PIXELS');
const budget = Math.sqrt(maxPixels / (guide.w * guide.h));
let scale = Math.min(moduleConst('ROI_UPSCALE'), Math.max(1, budget));
if (scale > budget) { scale = budget; }
const dw = Math.round(guide.w * scale), dh = Math.round(guide.h * scale);
const roi = cropScale(frame, W, H, guide.x, guide.y, guide.w, guide.h, dw, dh);
const roiText = decode(roi);
console.log(`  guide crop decode ${roi.width}x${roi.height} (scale ${scale.toFixed(2)}) -> ${roiText ? 'DECODED' : 'no barcode'}`);
ok(!!roiText, 'the guide crop at native pixels IS read');
ok(roiText === PAYLOAD, 'and the payload is the disk text the server parses');
ok(barcode.width / W < 0.25, 'the fixture barcode is the small guide-sized case measured in the video');

console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures) {
  console.log(`${failures} FAILED`);
  process.exit(1);
}
