"""Measure the on-page gap between the TAX and TOTAL INCL. VAT money columns.

Renders the invoice PDF with ghostscript at 200 dpi and looks at the actual ink
runs in a line-item row band, converting pixel columns back to PDF points.
Run:  .venv/bin/python scripts/measure_invoice_columns.py <pdf> <y_pdf_of_row>
"""
import subprocess
import sys
from pathlib import Path

from PIL import Image

PDF = sys.argv[1] if len(sys.argv) > 1 else 'test_invoice.pdf'
ROW_Y = float(sys.argv[2]) if len(sys.argv) > 2 else 406.0
DPI = 200
SCALE = DPI / 72.0
PAGE_HEIGHT_PT = 842.0


def render(pdf_path, png_path):
    subprocess.run([
        'gs', '-q', '-dSAFER', '-dBATCH', '-dNOPAUSE', '-sDEVICE=pnggray',
        f'-r{DPI}', '-dFirstPage=1', '-dLastPage=1',
        f'-sOutputFile={png_path}', pdf_path,
    ], check=True)


def main():
    png = Path('/tmp/abi_invoice_columns.png')
    render(PDF, png)
    image = Image.open(png).convert('L')
    width, height = image.size
    pixels = image.load()

    # The row band: text baseline is ROW_Y, glyphs sit just above it.
    top_px = int(round((PAGE_HEIGHT_PT - (ROW_Y + 8)) * SCALE))
    bottom_px = int(round((PAGE_HEIGHT_PT - (ROW_Y - 3)) * SCALE))
    x_start = int(round(380 * SCALE))
    x_end = int(round(565 * SCALE))

    inked = []
    for column in range(x_start, x_end):
        for row in range(top_px, bottom_px):
            if pixels[column, row] < 120:
                inked.append(column)
                break

    runs = []
    for column in inked:
        if runs and column - runs[-1][1] <= 3:
            runs[-1][1] = column
        else:
            runs.append([column, column])

    print(f'page {width}x{height}px at {DPI}dpi; row band y={ROW_Y}pt -> px {top_px}..{bottom_px}')
    for start, end in runs:
        print(f'  ink run x={start / SCALE:7.1f}..{end / SCALE:7.1f} pt  (width {(end - start) / SCALE:5.1f}pt)')
    for left, right in zip(runs, runs[1:]):
        gap = (right[0] - left[1]) / SCALE
        print(f'  gap after {left[1] / SCALE:7.1f}pt -> {right[0] / SCALE:7.1f}pt = {gap:5.1f}pt')


if __name__ == '__main__':
    main()
