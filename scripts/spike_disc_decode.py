#!/usr/bin/env python3
"""Licence-disc decode spike — picks the image -> barcode engine by measurement.

Programme phase 1 (A1). Deliberately **standalone**: it imports no app code, so the engine
decision rests on the engines alone and cannot inherit a bug from the parser under test.

What it does
------------
1. Runs every candidate engine over every image it can find, using the same variant ladder the
   app uses, and prints per-image / per-engine results: decoded?, payload length, first 200
   characters, decode time.
2. Builds **synthetic** PDF417 images from the parser fixtures and proves the
   image -> payload -> text round trip works. These are generated locally and are labelled as
   synthetic everywhere — they are not, and must never be presented as, a real licence disc.
3. Accepts a plain-text payload so a barcode read by a phone scanner app can be pasted in.

Image sources (in order): ``tests/fixtures/disc/``, ``~/disc-samples/``, any ``--image`` path,
and with ``--known-samples`` the real-world PDF417 photographs found on this machine during
recon. Those are **not copied into the repo**: they are photographs of people's driver's
licences, so committing them would put personal data in git (POPIA).

Usage
-----
    .venv/bin/python scripts/spike_disc_decode.py
    .venv/bin/python scripts/spike_disc_decode.py --known-samples
    .venv/bin/python scripts/spike_disc_decode.py --engines zxing-cpp --synthetic-only
    .venv/bin/python scripts/spike_disc_decode.py --text /tmp/barcode.txt
    .venv/bin/python scripts/spike_disc_decode.py --verbose
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "disc"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

#: Real PDF417 photographs found on this machine by recon. Kept out of git on purpose (they are
#: photographs of identity documents). Used only with --known-samples so the engine comparison
#: runs against real camera images, not just synthetic ones.
KNOWN_SAMPLES = (
    "/mnt/d/Hermes/Trailerpro/Archive/pdf417.PNG",
    "/mnt/d/Hermes/Trailerpro/Archive/ID.PNG",
    "/mnt/d/Hermes/Trailerpro/Archive/IDBACK.PNG",
    "/mnt/d/Hermes/Trailerpro/Archive/087a197e44f0c426e494c68a7f36874aaae62d9d.jpeg",
    "/mnt/d/Claude/walkin-app/public/test-assets/sa-licence-training/single_landscape.jpg",
    "/mnt/d/Claude/walkin-app/public/test-assets/sa-licence-training/single_small.jpg",
    "/mnt/d/Claude/walkin-app/public/test-assets/sa-licence-training/single_rotated_tall.jpg",
    "/mnt/d/Claude/walkin-app/public/test-assets/sa-licence-training/stacked_top.jpg",
    "/mnt/d/Claude/walkin-app/public/test-assets/sa-licence-training/stacked_middle.jpg",
    "/mnt/d/Claude/walkin-app/public/test-assets/sa-licence-training/stacked_bottom.jpg",
    "/mnt/d/Claude/walkin-app/public/test-assets/sa-license-test.jpeg",
)

# Duplicated from app/services/vehicle_disk.py on purpose (this script must not import the app);
# keep the two lists in step.
VARIANT_NAMES = (
    "original",
    "grayscale",
    "contrast-1.8",
    "contrast-2.5",
    "upscale-150",
    "upscale-2x",
    "upscale-3x-gray-contrast",
    "topcrop-40",
    "middlecrop-70",
    "rotate90",
    "rotate270",
)


def build_variants(image):
    from PIL import ImageEnhance, ImageOps

    rgb = image.convert("RGB")
    gray = ImageOps.grayscale(rgb)
    width, height = rgb.size
    return [
        ("original", rgb.copy()),
        ("grayscale", gray.copy()),
        ("contrast-1.8", ImageEnhance.Contrast(gray.copy()).enhance(1.8)),
        ("contrast-2.5", ImageEnhance.Contrast(gray.copy()).enhance(2.5)),
        ("upscale-150", rgb.resize((round(width * 1.5), round(height * 1.5)))),
        ("upscale-2x", rgb.resize((width * 2, height * 2))),
        (
            "upscale-3x-gray-contrast",
            ImageEnhance.Contrast(
                ImageOps.grayscale(rgb.resize((width * 3, height * 3)))
            ).enhance(2.0),
        ),
        ("topcrop-40", rgb.crop((0, 0, width, round(height * 0.40)))),
        ("middlecrop-70", rgb.crop((0, round(height * 0.15), width, round(height * 0.85)))),
        ("rotate90", rgb.rotate(90, expand=True)),
        ("rotate270", rgb.rotate(270, expand=True)),
    ]


def engine_zxing_cpp(image) -> list[str]:
    import zxingcpp

    results = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.PDF417)
    return [r.text for r in results if getattr(r, "text", "")]


def engine_pdf417decoder(image) -> list[str]:
    from pdf417decoder import PDF417Decoder

    decoder = PDF417Decoder(image.convert("RGB"))
    decoder.decode()
    return [
        bytes(payload).decode("utf-8", "replace")
        for payload in (getattr(decoder, "barcodes_data", None) or [])
    ]


ENGINES = {"zxing-cpp": engine_zxing_cpp, "pdf417decoder": engine_pdf417decoder}


def preview(payload: str) -> str:
    text = payload.replace("\r", "\\r").replace("\n", "\\n")
    printable = sum(1 for ch in payload if ch.isprintable() or ch in "\r\n\t")
    kind = "plain-text" if printable == len(payload) else "binary-or-non-text"
    return f"len={len(payload)} {kind} head={text[:200]!r}"


def scan_image(path: Path, engines: list[str], verbose: bool, budget_s: float) -> dict:
    from PIL import Image

    result = {"path": str(path), "size": None, "engines": {}}
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! cannot open: {type(exc).__name__}: {exc}")
        result["error"] = str(exc)
        return result

    result["size"] = f"{image.size[0]}x{image.size[1]} {image.mode}"
    variants = build_variants(image)
    print(f"  size={result['size']} variants={len(variants)}")

    for engine_name in engines:
        engine = ENGINES.get(engine_name)
        entry = {"decoded": False, "variant": None, "payloads": [], "seconds": 0.0}
        if engine is None:
            entry["error"] = "not installed / unknown"
            print(f"  [{engine_name}] SKIPPED (not installed)")
            result["engines"][engine_name] = entry
            continue
        started = time.perf_counter()
        for variant_name, variant_image in variants:
            if time.perf_counter() - started > budget_s:
                entry["error"] = f"time budget {budget_s:.0f}s exceeded"
                print(f"  [{engine_name}] stopped: time budget exceeded")
                break
            try:
                payloads = [p for p in engine(variant_image) if p and p.strip()]
            except ImportError as exc:
                entry["error"] = f"import failed: {exc}"
                print(f"  [{engine_name}] NOT INSTALLED ({exc})")
                break
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"    {engine_name}/{variant_name}: {type(exc).__name__}: {exc}")
                continue
            if payloads:
                entry.update(
                    decoded=True,
                    variant=variant_name,
                    payloads=[preview(p) for p in payloads],
                )
                print(f"  [{engine_name}] DECODED on variant '{variant_name}':")
                for payload in payloads:
                    print(f"      {preview(payload)}")
                break
            if verbose:
                print(f"    {engine_name}/{variant_name}: no barcode")
        entry["seconds"] = round(time.perf_counter() - started, 3)
        if not entry["decoded"]:
            print(f"  [{engine_name}] no decode in {entry['seconds']}s ({len(variants)} variants)")
        else:
            print(f"  [{engine_name}] decode time {entry['seconds']}s")
        result["engines"][engine_name] = entry
    return result


def make_synthetic_image(payload: str) -> bytes | None:
    try:
        import zxingcpp
    except ImportError:
        return None
    from PIL import Image

    barcode = zxingcpp.create_barcode(payload, zxingcpp.BarcodeFormat.PDF417)
    zxing_image = zxingcpp.write_barcode_to_image(barcode)
    height, width = zxing_image.shape
    pil = Image.frombuffer("L", (width, height), zxing_image, "raw", "L", 0, 1)
    buffer = io.BytesIO()
    pil.convert("RGB").resize((width * 2, height * 2)).save(buffer, format="PNG")
    return buffer.getvalue()


def synthetic_round_trip(engines: list[str], verbose: bool) -> None:
    payload_files = sorted(FIXTURE_DIR.glob("*.txt"))
    if not payload_files:
        print("no payload fixtures found in tests/fixtures/disc/")
        return
    print("\n== SYNTHETIC round trip (locally generated PDF417, NOT a real disc photo) ==")
    for path in payload_files:
        payload = path.read_text(encoding="utf-8")
        data = make_synthetic_image(payload)
        if data is None:
            print(f"  {path.name}: zxing-cpp not installed, cannot generate a synthetic image")
            continue
        print(f"  {path.name}: generated {len(data)} byte PNG for a {len(payload)} char payload")
        for engine_name in engines:
            engine = ENGINES.get(engine_name)
            if engine is None:
                print(f"    [{engine_name}] SKIPPED (not installed)")
                continue
            from PIL import Image

            image = Image.open(io.BytesIO(data))
            started = time.perf_counter()
            try:
                payloads = engine(image)
            except Exception as exc:  # noqa: BLE001
                print(f"    [{engine_name}] ERROR {type(exc).__name__}: {exc}")
                continue
            elapsed = (time.perf_counter() - started) * 1000
            match = any(p == payload for p in payloads)
            print(
                f"    [{engine_name}] decoded={bool(payloads)} exact-payload-match={match} "
                f"{elapsed:.1f}ms"
            )
            if verbose:
                for payload_found in payloads:
                    print(f"        {preview(payload_found)}")


def report_pasted_payload(text: str, label: str) -> None:
    print(f"\n== PASTED PAYLOAD ({label}) ==")
    print(f"  {preview(text)}")
    first_bytes = text.encode("utf-8", "replace")[:32]
    print(f"  first-bytes hex: {first_bytes.hex(' ')}")


def collect_images(extra: list[str], known: bool) -> list[Path]:
    found: list[Path] = []
    for directory in (FIXTURE_DIR, Path.home() / "disc-samples"):
        if directory.is_dir():
            found.extend(
                sorted(p for p in directory.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
            )
    found.extend(Path(p) for p in extra)
    if known:
        found.extend(Path(p) for p in KNOWN_SAMPLES)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", action="append", default=[], help="extra image path (repeatable)")
    parser.add_argument("--text", help="raw barcode payload file to report (or '-' for stdin)")
    parser.add_argument("--engines", default="zxing-cpp,pdf417decoder")
    parser.add_argument("--known-samples", action="store_true", help="include the real-world PDF417 photos")
    parser.add_argument("--synthetic-only", action="store_true", help="skip the image sweep")
    parser.add_argument("--no-synthetic", action="store_true", help="skip the synthetic round trip")
    parser.add_argument("--budget", type=float, default=180.0, help="seconds per image per engine")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    engines = [name.strip() for name in args.engines.split(",") if name.strip()]
    print("== licence-disc decode spike ==")
    print(f"python   : {sys.version.split()[0]}")
    print(f"repo     : {REPO_ROOT}")
    for name in engines:
        module = name.replace("-", "_")
        try:
            module_obj = __import__("zxingcpp" if module == "zxing_cpp" else module)
            version = getattr(module_obj, "__version__", None) or getattr(
                getattr(module_obj, "version", None), "__version__", "installed"
            )
            print(f"engine   : {name} -> installed ({version})")
        except ImportError:
            print(f"engine   : {name} -> NOT INSTALLED")
    print(f"variants : {len(VARIANT_NAMES)} ({', '.join(VARIANT_NAMES)})")

    if not args.synthetic_only:
        images = collect_images(args.image, args.known_samples)
        print(f"\n== IMAGE SWEEP: {len(images)} image(s) ==")
        if not images:
            print("  none found — add --image PATH, or drop photos in tests/fixtures/disc/")
        summary: dict[str, list[str]] = {name: [] for name in engines}
        for path in images:
            print(f"\n-- {path}")
            result = scan_image(path, engines, args.verbose, args.budget)
            for engine_name, entry in result["engines"].items():
                summary.setdefault(engine_name, []).append(
                    f"{path.name}: DECODED ({entry['variant']}, {entry['seconds']}s)"
                    if entry.get("decoded")
                    else f"{path.name}: no decode ({entry['seconds']}s)"
                )
        print("\n== SUMMARY ==")
        for engine_name, lines in summary.items():
            decoded = sum(1 for line in lines if ": DECODED" in line)
            print(f"  {engine_name}: {decoded}/{len(lines)} image(s) decoded")
            for line in lines:
                print(f"      {line}")

    if not args.no_synthetic:
        synthetic_round_trip(engines, args.verbose)

    if args.text:
        if args.text == "-":
            payload = sys.stdin.read()
            label = "stdin"
        else:
            payload = Path(args.text).read_text(encoding="utf-8")
            label = args.text
        report_pasted_payload(payload, label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
