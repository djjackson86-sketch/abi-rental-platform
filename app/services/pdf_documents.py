import math
import textwrap
import unicodedata
from pathlib import Path

from flask import current_app

from app.services.documents import display_document_label, display_document_number, document_accepted_stamp, document_date, document_datetime, document_has_rental_items, document_paid_stamp, document_tax_view, label_for, printable_document, rental_days_label
from app.services.customers import custom_fields_for
from app.services.settings import get_company_settings
from app.services.timezone import display_local_datetime


DOCUMENT_LOGO_STATIC_PATH = 'img/sano-trailers-logo.jpg'
# The document logo is 1200x510 with the ARTWORK starting 22px in (the JPG carries
# built-in white padding), so the image box sits ~1.7pt to the LEFT of the visible
# mark. Text aligned to the image edge therefore reads as indented against the
# logo - ~9pt out at x=36, which is exactly what the client spotted. The issuer
# and Bill To blocks align with the artwork instead, derived from the same numbers
# that place the image so the two can never drift apart.
LOGO_IMAGE_X = 25
LOGO_IMAGE_WIDTH = 92
LOGO_INK_LEFT_RATIO = 22 / 1200
LEFT_BLOCK_X = round(LOGO_IMAGE_X + LOGO_INK_LEFT_RATIO * LOGO_IMAGE_WIDTH, 2)
A4_PORTRAIT_WIDTH = 595
A4_PORTRAIT_HEIGHT = 842
A4_PORTRAIT_MEDIABOX = f'[0 0 {A4_PORTRAIT_WIDTH} {A4_PORTRAIT_HEIGHT}]'
# Invoice table money columns. The table runs from x=36 to x=559; TAX and
# TOTAL INCL. VAT were 35pt apart, so a TAX figure (R675.00 is ~30pt at 8pt)
# ran into the column beside it. 396 and 470 give a 74pt gutter while the
# widest amount the format can print still ends inside the table edge and the
# line-item total column aligns with the summary amount column.
TAX_COLUMN_X = 396
TOTAL_INCL_COLUMN_X = 470
INVOICE_TABLE_RIGHT_EDGE = 559


def _pdf_text(value):
    """Make a string safe for the PDF's single-byte font.

    The page stream is encoded as latin-1, so any character outside that range
    used to be replaced with "?" — which is exactly what the client saw as a
    question mark wherever a product name contained U+2044 FRACTION SLASH
    ("2.4m Utility Trailer - 1⁄2 ton", 23 live products).

    Typographic characters are transliterated to their plain ASCII equivalent
    (so a fraction slash prints a real "/"), and anything left over is
    decomposed and stripped of its accents before it can ever reach the stream.
    Characters that already encode cleanly are passed through untouched, so no
    existing document reflows.
    """
    text = str(value or '')
    if not text or text.isascii():
        return text
    text = text.translate(_PDF_TEXT_SUBSTITUTIONS)
    out = []
    for char in text:
        if ord(char) < 256:
            out.append(char)
            continue
        decomposed = unicodedata.normalize('NFKD', char)
        plain = ''.join(part for part in decomposed if not unicodedata.combining(part))
        out.append(plain if plain.isascii() else '?')
    return ''.join(out)


# Non-ASCII characters that appear in the client's own data (or are likely to),
# mapped to what they should look like in a printed document. ½ is a real
# Latin-1 glyph, but 1/2 keeps a document's plain-text copy-and-paste honest.
# str.maketrans keeps the readable character keys and builds the ordinal-keyed
# table str.translate actually needs (a str-keyed dict silently no-ops).
_PDF_TEXT_SUBSTITUTIONS = str.maketrans({
    '\u2044': '/',   # fraction slash — 23 live product names carry this
    '\u2215': '/',   # division slash
    '\u00bd': '1/2',  # ½
    '\u00bc': '1/4',
    '\u00be': '3/4',
    '\u2018': "'", '\u2019': "'",   # curly single quotes
    '\u201c': '"', '\u201d': '"',   # curly double quotes
    '\u2013': '-', '\u2014': '-',   # en/em dash
    '\u2026': '...',
    '\u00a0': ' ', '\u202f': ' ', '\u2009': ' ',   # non-breaking / thin spaces
    '\u2022': '*',
    '\u20ac': 'EUR',
})


def _escape_pdf_text(text):
    # The single choke point every text command goes through, so transliterating
    # here covers both page builders and the PAID stamp at once.
    return _pdf_text(text).replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _jpeg_dimensions(image_bytes):
    index = 2
    while index < len(image_bytes) - 9:
        if image_bytes[index] != 0xFF:
            index += 1
            continue
        marker = image_bytes[index + 1]
        index += 2
        if marker in (0xD8, 0xD9):
            continue
        length = int.from_bytes(image_bytes[index:index + 2], 'big')
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height = int.from_bytes(image_bytes[index + 3:index + 5], 'big')
            width = int.from_bytes(image_bytes[index + 5:index + 7], 'big')
            return width, height
        index += length
    raise ValueError('Unsupported JPEG logo dimensions')


def _document_logo_bytes():
    logo_path = Path(current_app.static_folder) / DOCUMENT_LOGO_STATIC_PATH
    if not logo_path.exists():
        return None
    return logo_path.read_bytes()


def _doc_value(document, key, default=None):
    try:
        return document[key]
    except (KeyError, IndexError):
        return default


def _simple_pdf(lines, logo_bytes=None):
    start_y = 680 if logo_bytes else 800
    leading = 18
    page_floor_y = 60
    # The loop below stops when the next line would fall off the page, so a long
    # document used to lose its tail silently. How many 18pt lines fit is fixed
    # arithmetic; anything longer continues on further pages (ticket ABI-341953022).
    lines_per_page = int((start_y - page_floor_y) // leading) + 1
    y = start_y
    content_lines = []
    image_object = None
    if logo_bytes:
        logo_width, logo_height = _jpeg_dimensions(logo_bytes)
        display_width = 130
        display_height = display_width * logo_height / logo_width
        content_lines.append(f'q {display_width:.2f} 0 0 {display_height:.2f} 50 {A4_PORTRAIT_HEIGHT - 50 - display_height:.2f} cm /Im1 Do Q')
        image_object = (
            f'<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\n'
        ).encode() + b'stream\n' + logo_bytes + b'\nendstream'
    if len(lines) > lines_per_page:
        streams = []
        for page_index, start in enumerate(range(0, len(lines), lines_per_page), start=1):
            page_lines = lines[start:start + lines_per_page]
            commands = list(content_lines)
            if page_index > 1:
                commands.append(_pdf_text_command(500, 740, f'Page {page_index}', size=10))
            commands.append('BT')
            y = start_y
            for line in page_lines:
                commands.append(_pdf_text_command(50, y, line, size=12))
                y -= leading
            commands.append('ET')
            streams.append('\n'.join(commands).encode('latin-1', 'replace'))
        return _pdf_objects(streams, image_object=image_object)
    stream_lines = content_lines + ['BT']
    for line in lines:
        # Absolute positioning: ``Td`` is a RELATIVE translate, so the old
        # ``50 {y} Td`` + ``-50 -18 Td`` pair walked the text matrix off the page
        # after the first line and everything below it rendered invisibly (the
        # text was in the stream, so a byte-level check could not see it).
        stream_lines.append(_pdf_text_command(50, y, line, size=12))
        y -= 18
        if y < 60:
            break
    stream_lines.append('ET')
    stream = '\n'.join(stream_lines).encode('latin-1', 'replace')
    objects = []
    objects.append(b'<< /Type /Catalog /Pages 2 0 R >>')
    objects.append(b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>')
    resources = b'/Font << /F1 4 0 R /F2 6 0 R >>'
    if image_object:
        resources += b' /XObject << /Im1 7 0 R >>'
    objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox ' + A4_PORTRAIT_MEDIABOX.encode() + b' /Resources << ' + resources + b' >> /Contents 5 0 R >>')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>')
    objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>')
    if image_object:
        objects.append(image_object)
    out = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f'{idx} 0 obj\n'.encode())
        out.extend(obj)
        out.extend(b'\nendobj\n')
    xref = len(out)
    out.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        out.extend(f'{offset:010d} 00000 n \n'.encode())
    out.extend(f'trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(out)



# --- dashboard day report (card layout) --------------------------------------
#
# The day report is a *document* the depot prints or files, so it carries the
# SANO wordmark, the depot and day it covers, and the name of the user who ran
# it. The figures themselves are untouched: ``cash.day_report_rows`` stays the
# one source of truth for both this PDF and the CSV export, so the two can
# never disagree.
#
# One page, budgeted rather than hoped for. The header and the two card grids
# have fixed geometry, so what is left for the three list sections is known
# before a single row is drawn: ``_report_line_allocations`` shares those slots
# round-robin (no section can starve another) and a section that does not get
# its full share prints "... and N more ... - see the CSV export" as its last
# row instead of dropping lines silently.
REPORT_LEFT = 36
REPORT_RIGHT = 559
REPORT_WIDTH = REPORT_RIGHT - REPORT_LEFT
REPORT_BOTTOM = 58
REPORT_HEADER_RULE_Y = 722
REPORT_FOOTER_Y = 40
REPORT_CARD_COLUMNS = 3
REPORT_CARD_GAP = 8
REPORT_CARD_WIDTH = (REPORT_WIDTH - (REPORT_CARD_COLUMNS - 1) * REPORT_CARD_GAP) / REPORT_CARD_COLUMNS
REPORT_CARD_HEIGHT = 36
REPORT_CARD_ROW_HEIGHT = 41
REPORT_SECTION_GAP = 16
REPORT_CARD_HEADING_DROP = 14
REPORT_LIST_HEADING_DROP = 12
REPORT_LIST_FIRST_ROW_DROP = 24
REPORT_LIST_LEADING = 13
REPORT_LIST_BOTTOM_PAD = 7
REPORT_LIST_ROW_PAD = 12
REPORT_LIST_VALUE_WIDTH = 96
REPORT_INK = '0.10 0.13 0.18'
REPORT_MUTED = '0.36 0.42 0.51'
REPORT_ACCENT = '0.08 0.39 1'
REPORT_CARD_LABEL = '0.35 0.42 0.52'
REPORT_CARD_FILL = '0.965 0.976 0.992'
REPORT_CARD_STROKE = '0.847 0.886 0.941'
REPORT_LIST_FILL = '0.980 0.984 0.992'
REPORT_RULE = '0.855 0.886 0.925'
# A short drawer is the one figure a manager scans for, so the variance card is
# the only card that changes colour. Every other card keeps the neutral accent.
REPORT_CARD_TONES = {
    'balanced': ('0.941 0.988 0.953', '0.373 0.741 0.510', '0.043 0.455 0.216', '0.145 0.600 0.298'),
    'over': ('0.941 0.988 0.953', '0.373 0.741 0.510', '0.043 0.455 0.216', '0.145 0.600 0.298'),
    'short': ('0.996 0.949 0.949', '0.878 0.400 0.400', '0.702 0.106 0.106', '0.780 0.090 0.090'),
}
REPORT_LOGO_WIDTH = 92
REPORT_LOGO_TOP = 800


def report_pdf_bytes(view):
    """The day report PDF: a branded, card-laid-out single page.

    ``view`` is ``cash.day_report_pdf_cards()`` — header meta plus card and list
    sections. A plain list of strings still renders through ``_simple_pdf`` (the
    previous contract), so no existing caller can be surprised by the change.

    Every string goes through ``_pdf_text_command`` -> ``_escape_pdf_text`` ->
    ``_pdf_text``, the single choke point that transliterates characters the
    single-byte WinAnsi font cannot print, so a note or a cash-used description
    typed on a phone cannot land on the page as "?".
    """
    if not isinstance(view, dict):
        return _simple_pdf([str(line) for line in view])
    return _report_template_pdf(view)


def _pdf_rect(x, y, width, height, fill=None, stroke=None, line_width=0.6):
    """A filled/stroked rectangle — the only panel primitive PDF actually has."""
    commands = ['q']
    if fill:
        commands.append(f'{fill} rg')
    if stroke:
        commands.append(f'{stroke} RG {line_width:.2f} w')
    commands.append(f'{x:.2f} {y:.2f} {width:.2f} {height:.2f} re')
    if fill and stroke:
        commands.append('B')
    elif fill:
        commands.append('f')
    else:
        commands.append('S')
    commands.append('Q')
    return ' '.join(commands)


def _pdf_text_width(text, size: int | float, bold=False):
    """Approximate Helvetica advance width — used to right-align and to wrap."""
    return len(str(text or '')) * size * (0.56 if bold else 0.5)


def _pdf_right_text(x_right, y, text, size: int | float = 9, font='F1'):
    return _pdf_text_command(x_right - _pdf_text_width(text, size, bold=font == 'F2'), y, text, size=size, font=font)


def _pdf_fit(text, size: int | float, width, bold=False):
    """Trim to ``width`` with a trailing ellipsis, so a cut is always visible."""
    text = str(text or '')
    if _pdf_text_width(text, size, bold) <= width:
        return text
    clipped = text
    while clipped and _pdf_text_width(f'{clipped}...', size, bold) > width:
        clipped = clipped[:-1]
    return f'{clipped.rstrip()}...' if clipped.strip() else '...'


def _pdf_wrap(text, size: int | float, width, bold=False, max_lines=2):
    """Word-wrap a free-text row (the page stream has no automatic wrapping).

    The last line is ellipsised if the text still does not fit, which keeps a
    long cash-used description or note inside its panel; the section's "see the
    CSV export" row states anything that had to be left out entirely.
    """
    words = str(text or '').split()
    if not words:
        return ['']
    lines = []
    remaining = list(words)
    while remaining:
        if len(lines) >= max_lines - 1:
            lines.append(_pdf_fit(' '.join(remaining), size, width, bold))
            break
        line = remaining.pop(0)
        while remaining and _pdf_text_width(f'{line} {remaining[0]}', size, bold) <= width:
            line = f'{line} {remaining.pop(0)}'
        lines.append(line if _pdf_text_width(line, size, bold) <= width else _pdf_fit(line, size, width, bold))
    return lines


def _report_card_section_height(card_count):
    """Vertical space a card grid takes, including its heading and its gap."""
    if card_count <= 0:
        return 0
    rows = math.ceil(card_count / REPORT_CARD_COLUMNS)
    return REPORT_SECTION_GAP + REPORT_CARD_HEADING_DROP + (rows - 1) * REPORT_CARD_ROW_HEIGHT + REPORT_CARD_HEIGHT


def _report_list_line_capacity(available_height, section_count):
    """How many row lines the list sections may draw in ``available_height``.

    Each list section costs a fixed 34pt (its heading, the panel's top padding
    and its bottom padding) plus 13pt per drawn line, so the line budget is the
    leftover height divided by the leading.
    """
    if section_count <= 0:
        return 0
    return max(0, int((available_height - 34 * section_count) // REPORT_LIST_LEADING))


def _report_line_allocations(desires, capacity):
    """Share ``capacity`` row lines out round-robin, so no section starves.

    Round-robin keeps a day with a dozen bank drop offs from blanking the
    cash-used section (or the end of day notes). A section that gets fewer lines
    than it asked for is truncated, and its last allocated line becomes the
    "... and N more" row — so every allocation is fully used and the total can
    never exceed ``capacity``.
    """
    allocations = [0] * len(desires)
    remaining = int(capacity)
    while remaining > 0:
        progressed = False
        for index, desire in enumerate(desires):
            if remaining <= 0:
                break
            if allocations[index] < desire:
                allocations[index] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return allocations


def _report_section_rows(entries, capacity, overflow_template):
    """The rows a section may draw, plus its stated overflow row (if any).

    Rows are never split across the boundary: a wrapped row that no longer fits
    whole is left for the overflow row to account for.
    """
    if capacity <= 0:
        return [], ''
    total = sum(len(entry['lines']) for entry in entries)
    if total <= capacity:
        return list(entries), ''
    available = capacity - 1
    drawn = []
    used = 0
    for entry in entries:
        if used + len(entry['lines']) > available:
            break
        drawn.append(entry)
        used += len(entry['lines'])
    hidden = len(entries) - len(drawn)
    if hidden <= 0:
        return drawn, ''
    return drawn, overflow_template.format(remaining=hidden)


def _report_row_entries(rows, label_width):
    """Wrap each row's label so the page budget can count the real drawn lines."""
    return [{
        'lines': _pdf_wrap(row.get('label'), 9, label_width, max_lines=2),
        'value_text': str(row.get('value') or ''),
    } for row in rows]


def _report_template_pdf(view):
    meta = view.get('meta') or {}
    sections = [section for section in (view.get('sections') or []) if section]
    card_sections = [section for section in sections if section.get('kind') != 'list']
    list_sections = [section for section in sections if section.get('kind') == 'list']

    # Work out the row layout of every list section BEFORE drawing anything, so
    # the single page is budgeted exactly rather than discovered by truncation.
    prepared = []
    for section in list_sections:
        has_value = any(row.get('value') for row in section.get('rows') or [])
        label_width = REPORT_RIGHT - 9 - (REPORT_LIST_VALUE_WIDTH if has_value else 0) - (REPORT_LEFT + REPORT_LIST_ROW_PAD)
        entries = _report_row_entries(section.get('rows') or [], label_width)
        prepared.append(entries)

    cards_height = sum(_report_card_section_height(len(section.get('cards') or [])) for section in card_sections)
    capacity = _report_list_line_capacity(REPORT_HEADER_RULE_Y - cards_height - REPORT_BOTTOM, len(list_sections))
    desires = []
    for section, entries in zip(list_sections, prepared):
        needed = sum(len(entry['lines']) for entry in entries)
        cap = section.get('cap')
        if cap:
            needed = min(needed, int(cap))
        desires.append(needed)
    allocations = _report_line_allocations(desires, capacity)

    text = ['BT']
    draw = []
    image_object = None
    logo_bytes = _document_logo_bytes()
    if logo_bytes:
        logo_width, logo_height = _jpeg_dimensions(logo_bytes)
        display_height = REPORT_LOGO_WIDTH * logo_height / logo_width
        draw.append(
            f'q {REPORT_LOGO_WIDTH:.2f} 0 0 {display_height:.2f} {REPORT_LEFT} '
            f'{REPORT_LOGO_TOP - display_height:.2f} cm /Im1 Do Q')
        image_object = (
            f'<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\n'
        ).encode() + b'stream\n' + logo_bytes + b'\nendstream'

    company = str(meta.get('company') or '').strip()
    text.append(_pdf_text_command(150, REPORT_LOGO_TOP, company, size=15, font='F2'))
    text.append(f'{REPORT_MUTED} rg')
    text.append(_pdf_text_command(150, REPORT_LOGO_TOP - 17, 'Daily dashboard report', size=10.5))
    text.append(f'{REPORT_INK} rg')
    text.append(_pdf_text_command(REPORT_LEFT, 750, f"Depot: {meta.get('depot') or '-'}", size=9.5))
    text.append(_pdf_text_command(REPORT_LEFT, 736, f"Business day: {meta.get('day') or '-'}", size=9.5))
    generated_at = display_local_datetime(meta.get('generated_at'))
    text.append(_pdf_text_command(380, 750, f"Generated: {generated_at}", size=9.5))
    text.append(_pdf_text_command(380, 736, f"Prepared by: {meta.get('prepared_by') or '-'}", size=9.5))
    draw.append(_pdf_rect(REPORT_LEFT, REPORT_HEADER_RULE_Y, REPORT_WIDTH, 1.2, fill=REPORT_RULE))

    y = REPORT_HEADER_RULE_Y
    for section in card_sections:
        cards = section.get('cards') or []
        if not cards:
            continue
        heading_y = y - REPORT_SECTION_GAP
        text.append(f'{REPORT_ACCENT} rg')
        text.append(_pdf_text_command(REPORT_LEFT, heading_y, str(section.get('title') or '').upper(), size=8.6, font='F2'))
        text.append(f'{REPORT_INK} rg')
        cards_top = heading_y - REPORT_CARD_HEADING_DROP
        row_count = math.ceil(len(cards) / REPORT_CARD_COLUMNS)
        for row in range(row_count):
            row_cards = cards[row * REPORT_CARD_COLUMNS:(row + 1) * REPORT_CARD_COLUMNS]
            # A part-filled last row is stretched across the full width so the
            # grid never ends on an empty gap (the dashboard has ten cards and
            # the cash up seven, so both end part-filled).
            card_width = (REPORT_WIDTH - (len(row_cards) - 1) * REPORT_CARD_GAP) / len(row_cards)
            for column, card in enumerate(row_cards):
                card_x = REPORT_LEFT + column * (card_width + REPORT_CARD_GAP)
                card_top = cards_top - row * REPORT_CARD_ROW_HEIGHT
                card_bottom = card_top - REPORT_CARD_HEIGHT
                tone = REPORT_CARD_TONES.get(str(card.get('tone') or ''))
                fill, stroke, value_colour, accent = tone or (REPORT_CARD_FILL, REPORT_CARD_STROKE, REPORT_INK, REPORT_ACCENT)
                draw.append(_pdf_rect(card_x, card_bottom, card_width, REPORT_CARD_HEIGHT, fill=fill, stroke=stroke))
                draw.append(_pdf_rect(card_x, card_bottom, 3, REPORT_CARD_HEIGHT, fill=accent))
                inner_width = card_width - 18
                label = _pdf_fit(card.get('label') or '', 6.6, inner_width)
                value = str(card.get('value') or '')
                value_size = 10.5
                while value_size > 7.0 and _pdf_text_width(value, value_size, bold=True) > inner_width:
                    value_size -= 0.5
                text.append(f'{REPORT_CARD_LABEL} rg')
                text.append(_pdf_text_command(card_x + 9, card_top - 13, label, size=6.6))
                text.append(f'{value_colour} rg')
                text.append(_pdf_text_command(card_x + 9, card_top - 28,
                                              _pdf_fit(value, value_size, inner_width, bold=True),
                                              size=value_size, font='F2'))
        text.append(f'{REPORT_INK} rg')
        y = cards_top - (row_count - 1) * REPORT_CARD_ROW_HEIGHT - REPORT_CARD_HEIGHT

    for section, entries, allocation in zip(list_sections, prepared, allocations):
        cap = section.get('cap')
        section_capacity = int(cap) if cap else allocation
        section_capacity = min(allocation, section_capacity)
        rows, overflow = _report_section_rows(entries, section_capacity, str(section.get('overflow') or ''))
        if not rows and not overflow:
            continue
        heading_y = y - REPORT_SECTION_GAP
        text.append(f'{REPORT_ACCENT} rg')
        text.append(_pdf_text_command(REPORT_LEFT, heading_y, str(section.get('title') or '').upper(), size=8.6, font='F2'))
        text.append(f'{REPORT_INK} rg')
        panel_top = heading_y - REPORT_LIST_HEADING_DROP
        baseline = heading_y - REPORT_LIST_FIRST_ROW_DROP
        for row_index, entry in enumerate(rows):
            row_baseline = baseline
            for line in entry['lines']:
                text.append(_pdf_text_command(REPORT_LEFT + REPORT_LIST_ROW_PAD, baseline, line, size=9))
                baseline -= REPORT_LIST_LEADING
            if entry['value_text']:
                text.append(_pdf_right_text(REPORT_RIGHT - 9, row_baseline, entry['value_text'], size=9, font='F2'))
            if row_index < len(rows) - 1 or overflow:
                draw.append(_pdf_rect(REPORT_LEFT + REPORT_LIST_ROW_PAD, baseline + 4, REPORT_WIDTH - (2 * REPORT_LIST_ROW_PAD), 0.6, fill=REPORT_RULE))
        if overflow:
            text.append(f'{REPORT_MUTED} rg')
            text.append(_pdf_text_command(REPORT_LEFT + REPORT_LIST_ROW_PAD, baseline, overflow, size=8.4))
            text.append(f'{REPORT_INK} rg')
            baseline -= REPORT_LIST_LEADING
        panel_bottom = baseline + REPORT_LIST_LEADING - REPORT_LIST_BOTTOM_PAD
        draw.append(_pdf_rect(REPORT_LEFT, panel_bottom, REPORT_WIDTH, panel_top - panel_bottom, fill=REPORT_LIST_FILL))
        draw.append(_pdf_rect(REPORT_LEFT, panel_bottom, 3, panel_top - panel_bottom, fill=REPORT_ACCENT))
        y = panel_bottom

    text.append(f'{REPORT_MUTED} rg')
    text.append(_pdf_right_text(REPORT_RIGHT, REPORT_FOOTER_Y, 'Page 1 of 1', size=7.5))
    text.append(f'{REPORT_INK} rg')
    text.append('ET')
    stream = '\n'.join(draw + text).encode('latin-1', 'replace')
    return _pdf_objects(stream, image_object=image_object)


def _compact_address(parts):
    return ', '.join(str(part) for part in parts if part)


def _pdf_text_command(x, y, text, size: int | float = 9, font='F1'):
    return f'/{font} {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_escape_pdf_text(text)}) Tj'


def _pdf_paid_stamp(x, y, text='PAID', size=30, angle=-18.0, colour=(0.78, 0.09, 0.09)):
    """A diagonal PAID stamp.

    PDF has no stamp primitive, so this rotates the coordinate system and draws
    a stroked box with the word inside it. Drawn before the text layer so any
    real content still sits on top.
    """
    radians = math.radians(angle)
    cos, sin = math.cos(radians), math.sin(radians)
    text_width = len(text) * size * 0.62
    width = text_width + (size * 0.9)
    height = size * 1.5
    pad_x = max(6.0, (width - text_width) / 2)
    pad_y = max(4.0, (height - (size * 0.72)) / 2)
    return [
        'q',
        f'{colour[0]:.3f} {colour[1]:.3f} {colour[2]:.3f} rg',
        f'{colour[0]:.3f} {colour[1]:.3f} {colour[2]:.3f} RG',
        f'{cos:.5f} {sin:.5f} {-sin:.5f} {cos:.5f} {x:.2f} {y:.2f} cm',
        # PDF `re` is (x y width height) - these were swapped, which drew a tall
        # sideways frame that reached up into the Order block.
        f'1.8 w 0 0 {width:.2f} {height:.2f} re S',
        'BT',
        f'/F2 {size} Tf {pad_x:.2f} {pad_y:.2f} Td ({_escape_pdf_text(text)}) Tj',
        'ET',
        'Q',
    ]


def _pdf_light_blue_rect(x, y, width, height):
    return f'q 0.86 0.94 1 rg {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f Q'


def _add_pdf_lines(commands, x, y, lines, size: int | float = 9, leading=14, max_lines=None, font='F1'):
    for index, line in enumerate(lines):
        if max_lines is not None and index >= max_lines:
            break
        commands.append(_pdf_text_command(x, y - (index * leading), line, size=size, font=font))


def _wrap_pdf_cell_text(value, max_chars=27, max_lines=2):
    """Wrap a PDF table cell without letting text run into the next column.

    The hand-built invoice PDF has fixed columns and Helvetica, so this uses a
    conservative character budget instead of pretending PDF has table layout.
    """
    text = ' '.join(str(value or '').split())
    if not text:
        return ['']
    return textwrap.wrap(text, width=max_chars, max_lines=max_lines, placeholder='…') or ['']


def _pdf_objects(stream, image_object=None):
    streams = stream if isinstance(stream, (list, tuple)) else [stream]
    page_ids = [3 + (index * 2) for index in range(len(streams))]
    content_ids = [page_id + 1 for page_id in page_ids]
    font_regular_id = 3 + (len(streams) * 2)
    font_bold_id = font_regular_id + 1
    image_id = font_bold_id + 1 if image_object else None
    resources = f'/Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >>'.encode()
    if image_id:
        resources += f' /XObject << /Im1 {image_id} 0 R >>'.encode()

    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        f'<< /Type /Pages /Kids [{" ".join(f"{page_id} 0 R" for page_id in page_ids)}] /Count {len(streams)} >>'.encode(),
    ]
    for page_id, content_id, page_stream in zip(page_ids, content_ids, streams):
        objects.append(
            b'<< /Type /Page /Parent 2 0 R /MediaBox ' + A4_PORTRAIT_MEDIABOX.encode()
            + b' /Resources << ' + resources + b' >> /Contents ' + f'{content_id} 0 R'.encode() + b' >>'
        )
        objects.append(b'<< /Length ' + str(len(page_stream)).encode() + b' >>\nstream\n' + page_stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>')
    if image_object:
        objects.append(image_object)
    out = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f'{idx} 0 obj\n'.encode())
        out.extend(obj)
        out.extend(b'\nendobj\n')
    xref = len(out)
    out.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:
        out.extend(f'{offset:010d} 00000 n \n'.encode())
    out.extend(f'trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(out)


def _invoice_template_pdf(document, items, settings, logo_bytes=None):
    display_label = display_document_label(document)
    display_number = display_document_number(document)
    issuer_name = document['branch_name'] or settings['company_name']
    issuer_email = document['branch_email'] or settings['email']
    issuer_phone = document['branch_phone'] or settings['phone']
    issuer_address = [
        document['branch_address_line1'] or settings['address_line1'],
        document['branch_address_line2'] or settings['address_line2'],
        document['branch_city'] or settings['city'],
        ' '.join(part for part in [document['branch_province'] or settings['province'], document['branch_postal_code'] or settings['postcode']] if part),
    ]
    customer_address = [
        document['customer_address_line1'], document['customer_address_line2'], document['customer_suburb'],
        document['customer_city'], ' '.join(part for part in [document['customer_province'], document['customer_postal_code']] if part),
        document['customer_country'],
    ]
    custom_fields = custom_fields_for(document)
    rent_label = rental_days_label(document)
    image_object = None
    logo_draw_command = None
    draw_commands = []
    if logo_bytes:
        logo_width, logo_height = _jpeg_dimensions(logo_bytes)
        display_width = LOGO_IMAGE_WIDTH
        display_height = display_width * logo_height / logo_width
        # The mark is a wide lockup (SANO tiles over TRAILERS) with a tight crop, so the
        # image box is anchored so that the INK lands exactly where the old padded asset's
        # ink sat (top-left, directly above the issuer/branch wording). Anchoring by the
        # image edge instead would let the branch-name line collide with the artwork.
        logo_bottom = A4_PORTRAIT_HEIGHT - 104
        logo_draw_command = f'q {display_width:.2f} 0 0 {display_height:.2f} {LOGO_IMAGE_X} {logo_bottom:.2f} cm /Im1 Do Q'
        draw_commands.append(logo_draw_command)
        image_object = (
            f'<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\n'
        ).encode() + b'stream\n' + logo_bytes + b'\nendstream'

    # Settled invoices are stamped PAID, and accepted quotes are stamped
    # ACCEPTED. Drawn first so every real figure sits on top of it, and centred in the empty band at the TOP of the page -
    # between the issuer block on the left and the document stack on the right.
    # It used to sit at (395, 498), which is the first item rows: the table text
    # and the light blue header band printed over it and buried it (client
    # report, ORD-10169 proforma).
    if document_paid_stamp(document):
        draw_commands.extend(_pdf_paid_stamp(233, 782, colour=(0.07, 0.54, 0.30)))
    elif document_accepted_stamp(document):
        draw_commands.extend(_pdf_paid_stamp(201, 782, text='ACCEPTED', size=26, colour=(0.09, 0.38, 0.70)))

    text_commands = ['BT']
    # Top-left brand/address, matching the supplied template.
    _add_pdf_lines(text_commands, LEFT_BLOCK_X, 715, [
        issuer_name,
        *[line for line in [issuer_phone, issuer_email] if line],
        *[line for line in issuer_address if line],
    ], size=8.2, leading=12, max_lines=7)

    # Top-right invoice and order stack.
    detail_x = 455
    text_commands.append(_pdf_text_command(detail_x, 760, display_label, size=8.5, font='F2'))
    invoice_lines = []
    if display_number:
        invoice_lines.append(display_number)
    invoice_lines.extend([f'{display_label} date:', document_date(document["created_at"])])
    _add_pdf_lines(text_commands, detail_x, 746, invoice_lines, size=8.5, leading=14)

    text_commands.append(_pdf_text_command(detail_x, 625, 'Order', size=8.5, font='F2'))
    order_lines = [f'Order: {document["order_number"]}']
    if document_has_rental_items(items):
        order_lines.extend([
            f'Pickup: {document_datetime(document["start_at"])}',
            f'Return: {document_datetime(document["end_at"])}',
            rent_label,
        ])
    _add_pdf_lines(text_commands, detail_x, 611, order_lines, size=8.5, leading=14)

    customer_lines = [
        'Bill To:',
        document['customer_name'] or '-',
        document['customer_email'] or '-',
    ]
    if document['customer_phone']:
        customer_lines.append(document['customer_phone'])
    customer_lines.extend([line for line in customer_address if line])
    vehicle_lines = []
    if custom_fields.get('vehicle_make'):
        vehicle_lines.append(f'Vehicle Make: {custom_fields["vehicle_make"]}')
    if custom_fields.get('vehicle_color'):
        vehicle_lines.append(f'Vehicle Color: {custom_fields["vehicle_color"]}')
    if custom_fields.get('vehicle_reg_no'):
        vehicle_lines.append(f'Veh Reg No: {custom_fields["vehicle_reg_no"]}')
    if vehicle_lines:
        customer_lines.extend(['', *vehicle_lines])
    alt_lines = []
    if custom_fields.get('alternative_contact_name'):
        alt_lines.append(f'Alternative Contact Name: {custom_fields["alternative_contact_name"]}')
    if custom_fields.get('alternative_contact_number'):
        alt_lines.append(f'Alternative Contact Number: {custom_fields["alternative_contact_number"]}')
    if custom_fields.get('alternative_contact_relationship'):
        alt_lines.append(f'Alternative Contact Relationship: {custom_fields["alternative_contact_relationship"]}')
    if alt_lines:
        customer_lines.extend(['', *alt_lines])
    visible_customer_lines = customer_lines[:18]
    # Bill To block, aligned under the logo/brand on the left.
    if visible_customer_lines:
        text_commands.append(_pdf_text_command(LEFT_BLOCK_X, 625, visible_customer_lines[0], size=8.5, font='F2'))
        _add_pdf_lines(text_commands, LEFT_BLOCK_X, 612, visible_customer_lines[1:], size=8.5, leading=13)
    customer_bottom_y = 625 - ((len(visible_customer_lines) - 1) * 13 if visible_customer_lines else 0)

    # Invoice table and totals.
    # Move the table up under the address blocks; if there are more than six
    # line items, page 1 stays readable and the remaining rows continue on the
    # following pages - each with the same column headings and a page number -
    # before the summary/banking block. Every item is always printed
    # (ticket ABI-341953022: the table used to stop after eight rows, so items
    # added by an order edit never reached the invoice/quote PDF).
    table_y = min(545, customer_bottom_y - 28)
    tax_view = document_tax_view(document, items)

    # Summary: total without VAT, the VAT itself, then the total with VAT.
    # Built before the table is drawn because how many summary rows there are
    # sets the floor the last item row of every page has to clear - that floor
    # is what tells the pagination how many rows genuinely fit on a page.
    totals = [('Total without VAT', f"R{tax_view['net']:.2f}")]
    if tax_view['discount']:
        discount_label = 'Discount'
        if _doc_value(document, 'discount_mode', '') == 'percent' and float(_doc_value(document, 'discount_value') or 0):
            discount_label = f'Discount ({float(_doc_value(document, "discount_value") or 0):g}%)'
        elif _doc_value(document, 'discount_mode', '') == 'amount' and float(_doc_value(document, 'discount_value') or 0):
            discount_label = f'Discount (R{float(_doc_value(document, "discount_value") or 0):.2f})'
        totals.append((discount_label, f"-R{tax_view['discount']:.2f}"))
    totals.append((f"VAT ({tax_view['rate']:g}%)" if tax_view['rate'] else 'VAT', f"R{tax_view['vat']:.2f}"))
    totals.append(('Total with VAT', f"R{tax_view['gross']:.2f}"))
    if (_doc_value(document, 'deposit_option', 'security_deposit') or 'security_deposit') == 'security_deposit':
        deposit_total = float(document["deposit_total"] or 0)
        if deposit_total:
            totals.append(('Security deposit', f'R{deposit_total:.2f}'))
    elif float(_doc_value(document, 'damage_waiver_amount') or 0):
        totals.append(('Damage waiver', f'R{float(_doc_value(document, "damage_waiver_amount") or 0):.2f}'))
    # Security deposit consumed by the return settlement (extra time, damages or
    # outstanding balance). It is already inside Paid as a deposit_applied
    # payment; this is the visible deduction the client asked for. Invoices only:
    # a quote never has a settled deposit.
    deposit_used = float(_doc_value(document, 'deposit_applied_amount') or 0)
    if _doc_value(document, 'document_type', '') == 'invoice' and deposit_used:
        totals.append(('Less: deposit used', f'-R{deposit_used:.2f}'))
    totals.extend([
        ('Paid', f'R{float(document["paid_total"] or 0):.2f}'),
        ('Amount due', f'R{float(document["due_total"] or 0):.2f}'),
    ])

    def add_table_header(draw, text, header_y):
        draw.append(_pdf_rect(36, header_y - 5, 523, 18, fill='0 0 0'))
        text.append('1 1 1 rg')
        _add_pdf_lines(text, 36, header_y, ['PRODUCT'], size=7.5)
        _add_pdf_lines(text, 185, header_y, ['QTY'], size=7.5)
        _add_pdf_lines(text, 220, header_y, ['DAYS'], size=7.5)
        _add_pdf_lines(text, 255, header_y, ['RATE'], size=7.5)
        _add_pdf_lines(text, 335, header_y, ['SUBTOTAL'], size=7.5)
        _add_pdf_lines(text, TAX_COLUMN_X, header_y, ['TAX'], size=7.5)
        _add_pdf_lines(text, TOTAL_INCL_COLUMN_X, header_y, ['TOTAL INCL. VAT'], size=7.5)
        text.append('0 0 0 rg')

    def add_item_rows(text, page_items, start_index, first_row_y):
        y_pos = first_row_y
        for offset, item in enumerate(page_items):
            line_index = start_index + offset
            name = item['product_name'] or item['custom_name'] or 'Item'
            sku = item['product_sku'] or ''
            line_view = tax_view['lines'][line_index] if line_index < len(tax_view['lines']) else {
                'unit_excl': 0.0, 'subtotal_excl': 0.0, 'tax': 0.0, 'total_incl': 0.0, 'rental_days': None}
            days_text = str(line_view.get('rental_days')) if line_view.get('rental_days') else '-'
            product_lines = _wrap_pdf_cell_text(name, max_chars=34, max_lines=3)
            if sku and len(product_lines) < 3:
                product_lines.append(str(sku)[:34])
            _add_pdf_lines(text, 36, y_pos, product_lines, size=8, leading=10, max_lines=3)
            _add_pdf_lines(text, 185, y_pos, [str(item['quantity'])], size=8)
            _add_pdf_lines(text, 220, y_pos, [days_text], size=8)
            _add_pdf_lines(text, 255, y_pos, [f"R{line_view['unit_excl']:.2f}"], size=8)
            _add_pdf_lines(text, 335, y_pos, [f"R{line_view['subtotal_excl']:.2f}"], size=8)
            _add_pdf_lines(text, TAX_COLUMN_X, y_pos, [f"R{line_view['tax']:.2f}"], size=8)
            _add_pdf_lines(text, TOTAL_INCL_COLUMN_X, y_pos, [f"R{line_view['total_incl']:.2f}"], size=8)
            y_pos -= 43
        return y_pos

    # Client rule: at most six line items on page 1. Everything after that is
    # printed on continuation pages, each repeating the column headings and
    # carrying `Page n`. A page takes as many rows as really fit between its
    # header and the summary band (43pt per row), and rows are chunked until the
    # list is exhausted - items are never truncated, however many an order edit
    # adds.
    first_page_limit = 6
    continuation_table_y = 675
    summary_floor_y = 58 + ((len(totals) - 1) * 14)
    # Lowest y the LAST item row of a page may reach: below it the summary rows
    # (14pt apart), their 12pt gap and the row pitch itself would not fit.
    last_row_y_floor = summary_floor_y + 12 + 43

    def rows_that_fit(first_row_y):
        return max(1, int((first_row_y - last_row_y_floor) // 43) + 1)

    first_page_items = list(items[:first_page_limit])
    remaining_items = list(items[first_page_limit:])
    add_table_header(draw_commands, text_commands, table_y)
    y = add_item_rows(text_commands, first_page_items, 0, table_y - 24)

    streams = []
    page_number = 1
    next_item_index = len(first_page_items)
    while remaining_items:
        page_number += 1
        text_commands.append('ET')
        streams.append('\n'.join(draw_commands + text_commands).encode('latin-1', 'replace'))
        page_items = remaining_items[:rows_that_fit(continuation_table_y - 24)]
        remaining_items = remaining_items[len(page_items):]
        draw_commands = [logo_draw_command] if logo_draw_command else []
        text_commands = ['BT']
        text_commands.append(_pdf_text_command(455, 760, f'{display_label} {display_number}', size=8.5, font='F2'))
        text_commands.append(_pdf_text_command(455, 746, f'Page {page_number}', size=8.5))
        add_table_header(draw_commands, text_commands, continuation_table_y)
        y = add_item_rows(text_commands, page_items, next_item_index, continuation_table_y - 24)
        next_item_index += len(page_items)

    # Keep the summary attached to the visible line items on the page that holds
    # the final item rows, clamped only far enough to keep Amount due on-page.
    totals_y = y - 12
    bank_lines = ['Banking details']
    for key, value in [
        ('Bank', document['branch_bank_name']),
        ('Account holder', document['branch_bank_account_name']),
        ('Account number', document['branch_bank_account_number']),
        ('Branch code', document['branch_bank_branch_code']),
        ('Account type', document['branch_bank_account_type']),
        ('Reference', document['branch_bank_reference_note']),
    ]:
        if value:
            bank_lines.append(f'{key}: {value}')
    summary_min_y = 58 + ((len(totals) - 1) * 14)
    totals_y = max(summary_min_y, totals_y)
    draw_commands.append(_pdf_rect(382, totals_y - ((len(totals) - 1) * 14) - 5, 177, (len(totals) * 14) + 4, stroke='0.82 0.86 0.91', line_width=0.6))
    for index, (label, amount) in enumerate(totals):
        line_y = totals_y - (index * 14)
        if label == 'Total with VAT':
            text_commands.append(_pdf_text_command(390, line_y, label, size=8.8, font='F2'))
            text_commands.append(_pdf_text_command(TOTAL_INCL_COLUMN_X, line_y, amount, size=8.8, font='F2'))
        else:
            text_commands.append(_pdf_text_command(390, line_y, label, size=8.8))
            text_commands.append(_pdf_text_command(TOTAL_INCL_COLUMN_X, line_y, amount, size=8.8))
    bank_y = totals_y - (len(totals) * 14) - 26
    bank_detail_lines = bank_lines[1:]
    bank_last_y = bank_y - 13 - ((len(bank_detail_lines) - 1) * 13 if bank_detail_lines else 0)
    if bank_last_y < 58:
        text_commands.append('ET')
        streams.append('\n'.join(draw_commands + text_commands).encode('latin-1', 'replace'))
        bank_draw_commands = [logo_draw_command] if logo_draw_command else []
        bank_text_commands = ['BT']
        bank_text_commands.append(_pdf_text_command(455, 760, f'{display_label} {display_number}', size=8.5, font='F2'))
        bank_text_commands.append(_pdf_text_command(455, 746, f'Page {len(streams) + 1}', size=8.5))
        bank_text_commands.append(_pdf_text_command(36, 715, 'Thank you for your business.', size=8.8, font='F2'))
        bank_text_commands.append(_pdf_text_command(36, 690, 'Banking details', size=8.5, font='F2'))
        _add_pdf_lines(bank_text_commands, 36, 675, bank_detail_lines, size=8.5, leading=13, max_lines=7)
        bank_text_commands.append('ET')
        streams.append('\n'.join(bank_draw_commands + bank_text_commands).encode('latin-1', 'replace'))
        return _pdf_objects(streams, image_object=image_object)
    text_commands.append(_pdf_text_command(36, bank_y + 18, 'Thank you for your business.', size=8.8, font='F2'))
    text_commands.append(_pdf_text_command(36, bank_y, 'Banking details', size=8.5, font='F2'))
    _add_pdf_lines(text_commands, 36, bank_y - 13, bank_detail_lines, size=8.5, leading=13, max_lines=7)
    text_commands.append('ET')
    streams.append('\n'.join(draw_commands + text_commands).encode('latin-1', 'replace'))
    return _pdf_objects(streams, image_object=image_object)


def document_pdf_bytes(document_id):
    document, items = printable_document(document_id)
    if not document:
        raise ValueError('Document not found')
    label = label_for(document['document_type'])
    display_label = display_document_label(document)
    display_number = display_document_number(document)
    settings = get_company_settings()
    issuer_name = document['branch_name'] or settings['company_name']
    issuer_email = document['branch_email'] or settings['email']
    issuer_phone = document['branch_phone'] or settings['phone']
    issuer_address = [
        document['branch_address_line1'] or settings['address_line1'],
        document['branch_address_line2'] or settings['address_line2'],
        document['branch_city'] or settings['city'],
        ' '.join(part for part in [document['branch_province'] or settings['province'], document['branch_postal_code'] or settings['postcode']] if part),
    ]
    lines = [f'{display_label} {display_number}']
    if document['document_type'] == 'invoice':
        lines.append(f'Invoice date: {document_date(document["created_at"])}')
    lines.append(f'Issuer: {issuer_name}')
    if issuer_email:
        lines.append(f'Issuer email: {issuer_email}')
    if issuer_phone:
        lines.append(f'Issuer phone: {issuer_phone}')
    lines.extend([line for line in issuer_address if line])
    lines.append(f'Order: {document["order_number"]}')
    if document_has_rental_items(items):
        lines.extend([f'Pickup: {document_datetime(document["start_at"])}', f'Return: {document_datetime(document["end_at"])}'])
    if document['document_type'] == 'invoice' and document_has_rental_items(items):
        lines.append(rental_days_label(document))
        bank_lines = [
            ('Bank', document['branch_bank_name']),
            ('Account holder', document['branch_bank_account_name']),
            ('Account number', document['branch_bank_account_number']),
            ('Branch code', document['branch_bank_branch_code']),
            ('Account type', document['branch_bank_account_type']),
            ('Reference', document['branch_bank_reference_note']),
        ]
        added_heading = False
        for key, value in bank_lines:
            if value:
                if not added_heading:
                    lines.append('Banking details:')
                    added_heading = True
                lines.append(f'{key}: {value}')
    lines.extend([f'Customer: {document["customer_name"] or "-"}', f'Email: {document["customer_email"] or "-"}'])
    if document['document_type'] == 'invoice':
        if document['customer_phone']:
            lines.append(f'Phone: {document["customer_phone"]}')
        customer_address = [
            document['customer_address_line1'],
            document['customer_address_line2'],
            document['customer_suburb'],
            document['customer_city'],
            ' '.join(part for part in [document['customer_province'], document['customer_postal_code']] if part),
            document['customer_country'],
        ]
        compact_address = ', '.join(line for line in customer_address if line)
        if compact_address:
            lines.append(f'Customer address: {compact_address}')
        custom_fields = custom_fields_for(document)
        if custom_fields.get('vehicle_make'):
            lines.append(f'Vehicle Make: {custom_fields["vehicle_make"]}')
        if custom_fields.get('vehicle_color'):
            lines.append(f'Vehicle Color: {custom_fields["vehicle_color"]}')
        if custom_fields.get('vehicle_reg_no'):
            lines.append(f'Veh Reg No: {custom_fields["vehicle_reg_no"]}')
        if custom_fields.get('alternative_contact_name'):
            lines.append(f'Alternative Contact Name: {custom_fields["alternative_contact_name"]}')
        if custom_fields.get('alternative_contact_number'):
            lines.append(f'Alternative Contact Number: {custom_fields["alternative_contact_number"]}')
        if custom_fields.get('alternative_contact_relationship'):
            lines.append(f'Alternative Contact Relationship: {custom_fields["alternative_contact_relationship"]}')
    lines.append('')
    for item in items:
        lines.append(f'{item["product_name"] or item["custom_name"]} x {item["quantity"]} @ R{float(item["unit_price"] or 0):.2f} = R{float(item["line_total"] or 0):.2f}')
    lines.extend(['', f'Subtotal: R{float(document["subtotal"] or 0):.2f}'])
    if float(_doc_value(document, 'discount_total') or 0):
        lines.append(f'Discount: -R{float(_doc_value(document, "discount_total") or 0):.2f}')
    lines.append(f'Tax: R{float(document["tax_total"] or 0):.2f}')
    if (_doc_value(document, 'deposit_option', 'security_deposit') or 'security_deposit') == 'security_deposit':
        lines.append(f'Security deposit: R{float(document["deposit_total"] or 0):.2f}')
    lines.append(f'Total: R{float(document["total"] or 0):.2f}')
    if document['document_type'] == 'invoice':
        deposit_used = float(_doc_value(document, 'deposit_applied_amount') or 0)
        if deposit_used:
            lines.append(f'Less: deposit used: -R{deposit_used:.2f}')
        lines.extend([f'Paid: R{float(document["paid_total"] or 0):.2f}', f'Amount due: R{float(document["due_total"] or 0):.2f}'])
    logo_bytes = _document_logo_bytes()
    if document['document_type'] in {'invoice', 'quote'}:
        return _invoice_template_pdf(document, items, settings, logo_bytes=logo_bytes)
    return _simple_pdf(lines, logo_bytes=logo_bytes)


def document_pdf_filename(document):
    prefix = label_for(document['document_type']).upper().replace(' ', '-')
    number = (document['number'] or '').strip() or 'PROFORMA'
    return f'{prefix}-{number}.pdf'
