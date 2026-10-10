# Vendored PDF417 decoder — ZXing (@zxing/library)

Local copy of the browser build of [`@zxing/library`](https://github.com/zxing-js/library),
the same PDF417 engine the TrailerPro scanner reference uses. It is committed into the
app so the licence-disk scanner has **no runtime CDN dependency** and the exact bytes
that ship are the bytes that were reviewed.

| File | Upstream path | Version |
| --- | --- | --- |
| `zxing-library-0.21.3.min.js` | `@zxing/library/umd/index.min.js` | `0.21.3` (pinned) |
| `LICENSE-zxing-library-0.21.3.txt` | `@zxing/library/LICENSE` | `0.21.3` |

- **Version:** `0.21.3` (exact pin; npm `dependencies` in the source project uses `^0.21.3`).
- **License:** the shipped `LICENSE` file is the **Apache License 2.0** (verbatim copy kept
  alongside). Note upstream's `package.json` declares `"license": "MIT"` while the bundled
  `LICENSE` text is Apache-2.0; the more specific `LICENSE` file is treated as authoritative
  and is reproduced here unchanged.
- **Global:** the UMD build attaches `window.ZXing` (used by `static/js/scan-camera.js`).
- **Integrity:** `sha256(zxing-library-0.21.3.min.js) = d7cc8f69dd70bdcf3ac00c9ae572bf2acb9f4132ba379c72df842e4db918652d`

## How it was produced (reproducible)

```bash
npm install --no-audit --no-fund @zxing/library@0.21.3
cp node_modules/@zxing/library/umd/index.min.js  static/vendor/zxing/zxing-library-0.21.3.min.js
cp node_modules/@zxing/library/LICENSE           static/vendor/zxing/LICENSE-zxing-library-0.21.3.txt
```

## How it is used

`static/js/scan-camera.js` uses only these exports:

- `ZXing.PDF417Reader` — the decoder
- `ZXing.HTMLCanvasElementLuminanceSource`, `ZXing.BinaryBitmap`, `ZXing.HybridBinarizer` — canvas → bitmap
- `ZXing.DecodeHintType` — `TRY_HARDER`

The scanner draws one bounded frame at a time onto its own canvas and calls
`reader.decode(bitmap)` on a ~300 ms cadence, so it stays in control of the camera
lifecycle and never decodes a full-resolution phone photo.

To verify the vendored engine actually decodes, run:

```bash
python -m pytest tests/test_scan_camera_live_decode.py -q
```

which generates a synthetic PDF417 with `zxingcpp`, writes it as a grayscale PGM and
decodes it with this exact bundle in Node.
