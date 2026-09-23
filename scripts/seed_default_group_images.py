"""Seed the Sano default category photos into ``product_groups`` (programme phase 11 / §C1).

The 15 full-resolution photos harvested from the Sano Trailers site live (untracked) in
``static/img/trailer-categories/``. Only their **web-sized re-encodes** are committed — a JPEG
per category, at most 1200px wide and roughly 30–85 KB each, in
``static/img/trailer-categories-web/`` (guardrail 2: never commit the originals). This script reads
those re-encodes, matches each to a ``product_groups`` row by name, and stores the bytes on
``product_groups.image_blob`` with ``image_source='default:sano'`` — the defaults a staff member
can later replace on the group form.

Rules
-----
* **Idempotent.** A group that already has an image is left alone, so re-running the script never
  rewrites or re-orders anything. A group whose image is ``source='upload'`` (a photo a person
  chose) is *protected*: the seed never clobbers an upload.
* **Matching** is a token-subset on the slugged name: the file stem's tokens (minus the filler
  ``trailer``/``trailers``) must all appear in the group name's tokens. ``6ft-trailer.jpg`` matches
  a group whose name contains ``6ft``; ``double-axle-car-trailer.jpg`` matches ``Double Axle Car``
  and not ``Double Axle``. When several groups match, the most specific (fewest tokens, then lowest
  ``sort_order``) wins, so ``single-axle-trailer.jpg`` picks ``Single Axle`` over ``Single Axle
  Flatbed``. Files with no matching group — and groups with no matching file (e.g. *mobile kitchen
  trailers*, *bobcat trailers*, which have no usable photo) — are reported, never invented.
* **Dry run is the default.** ``--commit`` writes. This mirrors ``backfill_client_verified.py``.

Usage
-----
    .venv/bin/python scripts/seed_default_group_images.py                 # dry run, default DB
    .venv/bin/python scripts/seed_default_group_images.py --database /tmp/x.db --commit
    .venv/bin/python scripts/seed_default_group_images.py --source static/img/trailer-categories
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from app.db import get_db, now  # noqa: E402
from app.services import group_images  # noqa: E402

DEFAULT_SOURCE_DIR = ROOT / "static" / "img" / "trailer-categories-web"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}

#: Tokens dropped from a *file* stem before matching. Every Sano filename ends in ``-trailer``,
#: and the group names (e.g. "Single Axle") do not, so the filler would otherwise block the match.
FILLER_TOKENS = frozenset({"trailer", "trailers"})


def slug_tokens(text):
    """Lowercase tokens of a name: ``"Double Axle Car"`` -> ``{'double', 'axle', 'car'}``."""
    return {part for part in re.split(r"[^a-z0-9]+", (text or "").lower()) if part}


def filename_key_tokens(stem):
    """The distinguishing tokens of a file stem (filler dropped)."""
    return slug_tokens(stem) - FILLER_TOKENS


def match_group_for_file(file_stem, groups):
    """The best-matching group for one file, or ``None``.

    ``groups`` is an iterable of rows/dicts with ``name``, ``sort_order`` and ``id``. A match
    requires every filename token to be present in the group name; among matches the group with
    the fewest tokens (then lowest ``sort_order``, then lowest id) wins, so the most specific
    category gets the photo.
    """
    key = filename_key_tokens(file_stem)
    if not key:
        return None
    candidates = []
    for group in groups:
        if key <= slug_tokens(group["name"]):
            candidates.append(group)
    if not candidates:
        return None
    candidates.sort(key=lambda g: (len(slug_tokens(g["name"])), g["sort_order"] or 0, g["id"]))
    return candidates[0]


def seed_group_images(source_dir, commit=False):
    """Match every image in ``source_dir`` to a group and (optionally) store it.

    Returns a list of result dicts, one per image file: ``{file, group, outcome}`` where outcome is
    one of ``set``/``would-set``, ``already`` (a prior seed left a default), ``protected`` (a staff
    upload must not be clobbered) or ``no-match``. When ``commit`` is false nothing is written.
    """
    db = get_db()
    groups = db.execute("SELECT * FROM product_groups ORDER BY id").fetchall()
    results = []
    for file in sorted(Path(source_dir).glob("*")):
        if file.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        group = match_group_for_file(file.stem, groups)
        if group is None:
            results.append({"file": file.name, "group": None, "outcome": "no-match"})
            continue
        if group["image_blob"] is not None:
            if (group["image_source"] or "") == group_images.SOURCE_UPLOAD:
                results.append({"file": file.name, "group": group["name"], "outcome": "protected"})
            else:
                results.append({"file": file.name, "group": group["name"], "outcome": "already"})
            continue
        if commit:
            db.execute(
                "UPDATE product_groups SET image_blob = ?, image_mime = ?, image_filename = ?, "
                "image_source = ?, updated_at = ? WHERE id = ?",
                (
                    file.read_bytes(),
                    MIME_BY_EXTENSION.get(file.suffix.lower(), "image/jpeg"),
                    file.name,
                    group_images.SOURCE_DEFAULT_SANO,
                    now(),
                    group["id"],
                ),
            )
        results.append(
            {"file": file.name, "group": group["name"], "outcome": "set" if commit else "would-set"}
        )
    if commit:
        db.commit()
    return results


def _print_table(results):
    print(f"{'FILE':38s} {'GROUP':34s} OUTCOME")
    print("-" * 88)
    for result in results:
        group = result["group"] or "(no matching group)"
        print(f"{result['file']:38s} {group:34s} {result['outcome']}")
    counts = {}
    for result in results:
        counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
    print("-" * 88)
    print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Seed the Sano default category photos into product_groups.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE_DIR), help="directory of image files")
    parser.add_argument("--database", help="local sqlite database to work on (default: the app default)")
    parser.add_argument("--commit", action="store_true", help="write the changes (default is a dry run)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    config = {
        "TESTING": False,
        "SECRET_KEY": "seed",
        "ADMIN_EMAIL": "admin@abi.local",
        "ADMIN_PASSWORD": "admin123",
    }
    if args.database:
        config["DATABASE"] = args.database
    app = create_app(config)
    with app.app_context():
        results = seed_group_images(args.source, commit=args.commit)
        _print_table(results)
    print("\nDRY RUN - nothing written. Re-run with --commit to apply." if not args.commit else "Written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
