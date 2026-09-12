"""Regenerate the SANO favicon assets from the source logo.

The wordmark's top band is four red rounded tiles, one white letter each
(S, A, N, O). The favicon is the FIRST tile cropped square, which stays legible
at 16px where the full wordmark would be an unreadable smear.

Run from the repo root:   .venv/bin/python scripts/abi_make_favicon.py

NEVER modify static/img/sano-trailers-logo.jpg — a test asserts on that exact
filename. This script only reads it.
"""
from pathlib import Path

from PIL import Image

IMG_DIR = Path(__file__).resolve().parent.parent / "static" / "img"
SRC = IMG_DIR / "sano-trailers-logo.jpg"


def _is_red(pixel):
    r, g, b = pixel
    return r > 110 and g < 110 and b < 110


def build():
    image = Image.open(SRC).convert("RGB")
    px = image.load()
    width, height = image.size

    # The letter tiles are the only strongly-red band in the mark.
    band_rows = [
        y for y in range(height)
        if sum(1 for x in range(width) if _is_red(px[x, y])) > 40
    ]
    # Keep only the FIRST contiguous run of red-heavy rows - that is the tile band. A wide
    # wordmark can also carry a red dash band lower down (either side of "TRAILERS"), and
    # taking min..max would drag the crop down over that text and leak black/red fragments
    # into the icon.
    band_top = band_rows[0]
    band_bottom = band_top
    for y in band_rows[1:]:
        if y > band_bottom + 1:
            break
        band_bottom = y

    red_columns = [
        x for x in range(width)
        if sum(1 for y in range(band_top, band_bottom + 1) if _is_red(px[x, y])) >= 4
    ]

    # Group contiguous columns -> one run per letter tile.
    runs = []
    current = None
    for x in red_columns:
        if current and x == current[1] + 1:
            current[1] = x
        else:
            current = [x, x]
            runs.append(current)
    print(f"letter tile runs: {runs}")

    left, right = runs[0]
    tile = image.crop((left, band_top, right + 1, band_bottom + 1))

    # Square it up around the centre, keeping the tile's own white padding.
    side = max(tile.size)
    square = Image.new("RGB", (side, side), (255, 255, 255))
    square.paste(tile, ((side - tile.size[0]) // 2, (side - tile.size[1]) // 2))

    square.resize((180, 180), Image.LANCZOS).save(IMG_DIR / "favicon-180.png")
    square.resize((32, 32), Image.LANCZOS).save(IMG_DIR / "favicon-32.png")
    square.resize((16, 16), Image.LANCZOS).save(IMG_DIR / "favicon-16.png")
    square.resize((512, 512), Image.LANCZOS).save(IMG_DIR / "favicon-512.png")
    square.save(IMG_DIR / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    for name in ["favicon.ico", "favicon-16.png", "favicon-32.png", "favicon-180.png", "favicon-512.png"]:
        path = IMG_DIR / name
        print(f"wrote {name} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
