"""Ticket ABI-341953072 live smoke (read-only).

Signs in as the main profile, checks the Orders page's new Unsent Invoices card,
downloads a real invoice PDF and measures the thank-you/banking gap plus the
totals right edges on the live bytes. Renders the live PDF to PNG for a visual
look. Writes no production data.

Run: .venv/bin/python scripts/abi_372_live_smoke.py [document_id]
"""
import re
import subprocess
import sys
from pathlib import Path

import requests

BASE = 'https://abi-rental-platform.onrender.com'
KEYS = Path('/mnt/d/Hermes/API keys.txt')
OUT = Path('/tmp/abi372_live')


def main_password():
    text = KEYS.read_text(encoding='utf-8', errors='replace')
    for line in text.splitlines():
        if 'Head office admin' in line:
            tokens = re.findall(r'\(([^()]*)\)', line)
            if tokens:
                return tokens[-1].strip().split()[-1]
    raise SystemExit('main profile password line not found')


def main():
    OUT.mkdir(exist_ok=True)
    session = requests.Session()
    login_page = session.get(f'{BASE}/login', timeout=30)
    print('GET /login', login_page.status_code)
    options = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', login_page.text)
    account = next((value for value, label in options if 'Head office admin' in label), None)
    print('accounts on the login page:', len(options), '| main account found:', account)
    res = session.post(f'{BASE}/login', data={
        'user_id': account, 'password': main_password()}, timeout=30, allow_redirects=True)
    print('POST /login', res.status_code, '| landed on', res.url)

    orders = session.get(f'{BASE}/orders', timeout=60)
    print('GET /orders', orders.status_code, len(orders.content), 'bytes')
    html = orders.text
    cards = re.findall(
        r'<div class="([^"]*metric-card[^"]*)"><small>(.*?)</small><b>(.*?)</b></div>', html, re.S)
    print('cards:', [(label.strip(), value.strip(), cls.strip()) for cls, label, value in cards])
    unsent = next(((value, cls) for cls, label, value in cards if label.strip() == 'Unsent Invoices'), None)
    print('UNSENT CARD:', unsent)
    print('old grid css gone:', 'discount-label-wrap' not in html)

    documents = session.get(f'{BASE}/documents', timeout=60)
    ids = re.findall(r'/documents/(\d+)/download\.pdf', documents.text)
    print('GET /documents', documents.status_code, '| invoice links found:', len(ids))

    document_id = sys.argv[1] if len(sys.argv) > 1 else None
    if document_id is None:
        for candidate in ids:
            pdf = session.get(f'{BASE}/documents/{candidate}/download.pdf?view=1', timeout=60)
            if pdf.status_code == 200 and pdf.content.startswith(b'%PDF') and b'Banking details' in pdf.content:
                document_id = candidate
                break
    if document_id is None:
        print('no invoice PDF with a banking block found - skipping the PDF half')
        return

    pdf = session.get(f'{BASE}/documents/{document_id}/download.pdf?view=1', timeout=60)
    print(f'GET /documents/{document_id}/download.pdf', pdf.status_code, len(pdf.content), 'bytes')
    raw = pdf.content.decode('latin-1')
    runs = []
    for index, page in enumerate(re.findall(r'stream\n(.*?)\nendstream', raw, re.DOTALL), start=1):
        for font, size, x, y, text in re.findall(
                r'/(F\d) ([\d.]+) Tf 1 0 0 1 ([\d.]+) ([\d.]+) Tm \(((?:[^()\\]|\\.)*)\) Tj', page):
            runs.append((index, float(x), float(y), float(size), font, text))

    thank_you = [run for run in runs if run[5] == 'Thank you for your business.']
    banking = [run for run in runs if run[5] == 'Banking details']
    for page, x, y, _size, _font, _text in thank_you:
        same_page = [run for run in banking if run[0] == page]
        if same_page:
            print(f'page {page}: thank-you y={y}, banking y={same_page[0][2]}, '
                  f'GAP={round(y - same_page[0][2], 1)}pt')

    labels = {run[5]: run for run in runs if run[3] == 8.8 and run[1] > 380}
    amounts = {}
    for run in runs:
        if run[3] == 8.8 and run[1] > 380 and run[5].startswith(('R', '-R')):
            same = [lab for lab in labels.values() if abs(lab[2] - run[2]) < 0.01]
            if same:
                amounts[same[0][5]] = (run[5], run[1], run[3], run[4])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.services.pdf_documents import _pdf_exact_text_width
    edges = {label: round(x + _pdf_exact_text_width(text, size, bold=font == 'F2'), 2)
             for label, (text, x, size, font) in amounts.items()}
    print('totals amounts (label -> text, right edge):')
    for label, (text, x, size, font) in amounts.items():
        print(f'  {label!r}: {text} @x={x} font={font} -> right edge {edges[label]}')
    print('distinct right edges:', sorted(set(edges.values())))

    out = OUT / f'live_invoice_{document_id}.pdf'
    out.write_bytes(pdf.content)
    png = out.with_suffix('.png')
    subprocess.run(['gs', '-q', '-dNOPAUSE', '-dBATCH', '-sDEVICE=png16m', '-r150',
                    f'-sOutputFile={png}', str(out)], check=True)
    print('rendered', png)


if __name__ == '__main__':
    main()
