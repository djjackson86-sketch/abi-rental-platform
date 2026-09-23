import math
import textwrap
import unicodedata
from pathlib import Path

from flask import current_app

from app.services.documents import display_document_label, display_document_number, document_accepted_stamp, document_date, document_datetime, document_has_rental_items, document_paid_stamp, document_tax_view, label_for, printable_document, rental_days_label
from app.services.customers import custom_fields_for
from app.services.settings import get_company_settings
from app.services.timezone import display_local_date, display_local_datetime, local_now_iso


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
# Invoice table columns. The table aligns to the logo/text artwork on the left,
# while the compact money columns free space for long product names. Keep the
# final amount aligned with the summary amount column.
INVOICE_TABLE_X = LEFT_BLOCK_X
QTY_COLUMN_X = 238
DAYS_COLUMN_X = 268
RATE_COLUMN_X = 300
SUBTOTAL_COLUMN_X = 356
TAX_COLUMN_X = 410
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
# Multi-page, budgeted rather than hoped for. The header and the card grids
# have fixed geometry, then the variable list sections flow onto continuation
# pages. Rows are kept whole whenever possible and continuation pages repeat the
# depot/day context plus page numbering, so busy days do not silently lose text.
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


# Real Helvetica / Helvetica-Bold advance widths (AFM metrics, /1000 em) for the
# printable ASCII range. The flat 0.5/0.56 factors above are good enough for
# wrapping and right-alignment, but they are ~15% wide on a bold line: measuring
# a centred title with them left it visibly off the midline, which is the whole
# point of a centring fix. Only the centred helper uses these.
_HELVETICA_WIDTHS = dict(zip(
    ' ' + '!"#$%&\'()*+,-./' + '0123456789' + ':;<=>?@'
    + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' + '[\\]^_`' + 'abcdefghijklmnopqrstuvwxyz' + '{|}~',
    [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
     556, 556, 556, 556, 556, 556, 556, 556, 556, 556,
     278, 278, 584, 584, 584, 556, 1015,
     667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667,
     778, 722, 667, 611, 722, 667, 944, 667, 667, 611,
     278, 278, 278, 469, 556, 333,
     556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556,
     556, 333, 500, 278, 556, 500, 722, 500, 500, 500,
     334, 260, 334, 584]))
_HELVETICA_BOLD_WIDTHS = dict(zip(
    ' ' + '!"#$%&\'()*+,-./' + '0123456789' + ':;<=>?@'
    + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' + '[\\]^_`' + 'abcdefghijklmnopqrstuvwxyz' + '{|}~',
    [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
     556, 556, 556, 556, 556, 556, 556, 556, 556, 556,
     333, 333, 584, 584, 584, 611, 975,
     722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667,
     778, 722, 667, 611, 722, 667, 944, 667, 667, 611,
     333, 278, 333, 584, 556, 333,
     556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611,
     611, 389, 556, 333, 611, 556, 778, 556, 556, 500,
     389, 280, 389, 584]))


def _pdf_exact_text_width(text, size: int | float, bold=False):
    """The width a PDF viewer will actually use for a run.

    Measures the string the renderer will draw (``_pdf_text`` first, so
    transliteration is accounted for) with the real glyph widths. Accented
    Latin-1 characters fall back to the width of their base letter.
    """
    table = _HELVETICA_BOLD_WIDTHS if bold else _HELVETICA_WIDTHS
    adobe = 0
    for char in _pdf_text(text):
        width = table.get(char)
        if width is None:
            decomposed = unicodedata.normalize('NFKD', char)
            width = table.get(decomposed[0]) if decomposed else None
        adobe += width if width is not None else 556
    return adobe * float(size) / 1000.0


def _pdf_right_text(x_right, y, text, size: int | float = 9, font='F1'):
    return _pdf_text_command(x_right - _pdf_text_width(text, size, bold=font == 'F2'), y, text, size=size, font=font)


def _pdf_centre_text(y, text, size: int | float = 9, font='F1', centre=None):
    """Centre a run on the page width.

    Ticket ABI-341953027: the day report's title block used to start at a fixed
    x of 150, which reads off-centre on A4 portrait. Centring derives x from the
    text's own measured width, so the company name and the subtitle sit on the
    page's midline no matter how long either string is.
    """
    page_centre = A4_PORTRAIT_WIDTH / 2 if centre is None else centre
    x = page_centre - _pdf_exact_text_width(text, size, bold=font == 'F2') / 2
    return _pdf_text_command(x, y, text, size=size, font=font)


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

    ``max_lines=None`` is used by the dashboard report's multi-page list panels:
    once the report can continue to page 2, long notes/descriptions should wrap
    instead of being ellipsised on page 1. Fixed-height cards still pass a small
    limit and keep the visible ellipsis behaviour.
    """
    words = str(text or '').split()
    if not words:
        return ['']
    lines = []
    remaining = list(words)
    while remaining:
        if max_lines is not None and len(lines) >= max_lines - 1:
            lines.append(_pdf_fit(' '.join(remaining), size, width, bold))
            break
        line = remaining.pop(0)
        while remaining and _pdf_text_width(f'{line} {remaining[0]}', size, bold) <= width:
            line = f'{line} {remaining.pop(0)}'
        lines.append(line if _pdf_text_width(line, size, bold) <= width else _pdf_fit(line, size, width, bold))
    return lines


def _report_row_entries(rows, label_width):
    """Wrap each row's label so the page budget can count the real drawn lines."""
    return [{
        'lines': _pdf_wrap(row.get('label'), 9, label_width, max_lines=None),
        'value_text': str(row.get('value') or ''),
    } for row in rows]


def _report_logo_object():
    logo_bytes = _document_logo_bytes()
    if not logo_bytes:
        return None, []
    logo_width, logo_height = _jpeg_dimensions(logo_bytes)
    display_height = REPORT_LOGO_WIDTH * logo_height / logo_width
    logo_draw = [
        f'q {REPORT_LOGO_WIDTH:.2f} 0 0 {display_height:.2f} {REPORT_LEFT} '
        f'{REPORT_LOGO_TOP - display_height:.2f} cm /Im1 Do Q'
    ]
    image_object = (
        f'<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} '
        f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\n'
    ).encode() + b'stream\n' + logo_bytes + b'\nendstream'
    return image_object, logo_draw


def _report_header(meta, page_number=1, logo_draw=None):
    """Return (draw, text, y) for a report page header with context."""
    draw = list(logo_draw or [])
    text = ['BT']
    company = str(meta.get('company') or '').strip()
    # Ticket ABI-341953027: the title block is centred on the page midline.
    text.append(_pdf_centre_text(REPORT_LOGO_TOP, company, size=15, font='F2'))
    text.append(f'{REPORT_MUTED} rg')
    subtitle = 'Daily dashboard report' if page_number == 1 else 'Daily dashboard report (continued)'
    text.append(_pdf_centre_text(REPORT_LOGO_TOP - 17, subtitle, size=10.5))
    text.append(f'{REPORT_INK} rg')
    text.append(_pdf_text_command(REPORT_LEFT, 750, f"Depot: {meta.get('depot') or '-'}", size=9.5))
    text.append(_pdf_text_command(REPORT_LEFT, 736, f"Business day: {meta.get('day') or '-'}", size=9.5))
    generated_at = display_local_datetime(meta.get('generated_at'))
    text.append(_pdf_text_command(380, 750, f"Generated: {generated_at}", size=9.5))
    text.append(_pdf_text_command(380, 736, f"Prepared by: {meta.get('prepared_by') or '-'}", size=9.5))
    draw.append(_pdf_rect(REPORT_LEFT, REPORT_HEADER_RULE_Y, REPORT_WIDTH, 1.2, fill=REPORT_RULE))
    return draw, text, REPORT_HEADER_RULE_Y


def _report_new_page(pages, meta, logo_draw):
    draw, text, y = _report_header(meta, len(pages) + 1, logo_draw)
    page = {'draw': draw, 'text': text, 'y': y, 'empty': True}
    pages.append(page)
    return page


def _report_draw_card_sections(page, card_sections):
    text = page['text']
    draw = page['draw']
    y = page['y']
    for section in card_sections:
        cards = section.get('cards') or []
        if not cards:
            continue
        page['empty'] = False
        heading_y = y - REPORT_SECTION_GAP
        text.append(f'{REPORT_ACCENT} rg')
        text.append(_pdf_text_command(REPORT_LEFT, heading_y, str(section.get('title') or '').upper(), size=8.6, font='F2'))
        text.append(f'{REPORT_INK} rg')
        cards_top = heading_y - REPORT_CARD_HEADING_DROP
        row_count = math.ceil(len(cards) / REPORT_CARD_COLUMNS)
        for row in range(row_count):
            row_cards = cards[row * REPORT_CARD_COLUMNS:(row + 1) * REPORT_CARD_COLUMNS]
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
    page['y'] = y


def _report_list_capacity_lines(y):
    """How many row lines still fit below ``y`` on this page.

    The section gap is only worth its full 16pt when the section that needs it
    gets more than one line: a single-line section (an "no cash used recorded"
    row, say) would otherwise push a whole page break just to repeat itself, so
    the gap shrinks to whatever is left over.
    """
    room = y - REPORT_BOTTOM
    available = room - REPORT_SECTION_GAP
    if _report_section_lines(available) < 2:
        available = room - 2
    return _report_section_lines(available)


def _report_section_lines(available):
    usable = available - REPORT_LIST_FIRST_ROW_DROP + REPORT_LIST_LEADING - REPORT_LIST_BOTTOM_PAD
    return max(0, int(usable // REPORT_LIST_LEADING))


def _report_draw_list_chunk(page, section, rows, continued=False):
    text = page['text']
    draw = page['draw']
    page['empty'] = False
    heading_y = page['y'] - REPORT_SECTION_GAP
    title = str(section.get('title') or '').upper()
    if continued:
        title = f'{title} (CONTINUED)'
    text.append(f'{REPORT_ACCENT} rg')
    text.append(_pdf_text_command(REPORT_LEFT, heading_y, title, size=8.6, font='F2'))
    text.append(f'{REPORT_INK} rg')
    panel_top = heading_y - REPORT_LIST_HEADING_DROP
    baseline = heading_y - REPORT_LIST_FIRST_ROW_DROP
    for row_index, entry in enumerate(rows):
        lines = entry.get('lines') or ['']
        row_baseline = baseline
        for line in lines:
            text.append(_pdf_text_command(REPORT_LEFT + REPORT_LIST_ROW_PAD, baseline, line, size=9))
            baseline -= REPORT_LIST_LEADING
        if entry['value_text']:
            text.append(_pdf_right_text(REPORT_RIGHT - 9, row_baseline, entry['value_text'], size=9, font='F2'))
        if row_index < len(rows) - 1:
            draw.append(_pdf_rect(REPORT_LEFT + REPORT_LIST_ROW_PAD, baseline + 4, REPORT_WIDTH - (2 * REPORT_LIST_ROW_PAD), 0.6, fill=REPORT_RULE))
    panel_bottom = baseline + REPORT_LIST_LEADING - REPORT_LIST_BOTTOM_PAD
    draw.append(_pdf_rect(REPORT_LEFT, panel_bottom, REPORT_WIDTH, panel_top - panel_bottom, fill=REPORT_LIST_FILL))
    draw.append(_pdf_rect(REPORT_LEFT, panel_bottom, 3, panel_top - panel_bottom, fill=REPORT_ACCENT))
    page['y'] = panel_bottom


def _report_draw_list_segment(page, section, entries, entries_done, skip_lines):
    """Draw what fits on ``page`` starting at ``entries_done`` / ``skip_lines``.

    A row that no longer fits whole is left for the next page. The one exception
    is a single row taller than a whole page (a paragraph pasted into a note):
    it is split at a line boundary so its text still reaches paper instead of
    looping forever. Returns ``(index of the row being drawn, lines consumed of
    that row)`` so the caller can resume exactly where the page ran out.
    """
    capacity = _report_list_capacity_lines(page['y'])
    segment = []
    used = 0
    cursor = entries_done
    skip = skip_lines
    while cursor < len(entries):
        lines = entries[cursor].get('lines') or ['']
        pending = lines[skip:]
        if used + len(pending) > capacity:
            if segment:
                break
            if capacity <= 0:
                break
            take = max(1, min(len(pending), capacity))
            drawn_lines = skip + take
            segment.append({'lines': pending[:take], 'value_text': entries[cursor].get('value_text')})
            _report_draw_list_chunk(page, section, segment, continued=bool(entries_done) or skip > 0)
            if drawn_lines >= len(lines):
                return cursor + 1, 0
            return cursor, drawn_lines
        segment.append({'lines': pending, 'value_text': entries[cursor].get('value_text')})
        used += len(pending)
        cursor += 1
        skip = 0
    if not segment:
        return entries_done, skip_lines
    _report_draw_list_chunk(page, section, segment, continued=bool(entries_done))
    return cursor, 0

def _report_template_pdf(view):
    meta = view.get('meta') or {}
    sections = [section for section in (view.get('sections') or []) if section]
    card_sections = [section for section in sections if section.get('kind') != 'list']
    list_sections = [section for section in sections if section.get('kind') == 'list']

    prepared = []
    for section in list_sections:
        has_value = any(row.get('value') for row in section.get('rows') or [])
        label_width = REPORT_RIGHT - 9 - (REPORT_LIST_VALUE_WIDTH if has_value else 0) - (REPORT_LEFT + REPORT_LIST_ROW_PAD)
        prepared.append(_report_row_entries(section.get('rows') or [], label_width))

    image_object, logo_draw = _report_logo_object()
    pages = []
    page = _report_new_page(pages, meta, logo_draw)
    _report_draw_card_sections(page, card_sections)

    for section, entries in zip(list_sections, prepared):
        entries_done = 0
        skip_lines = 0
        while entries_done < len(entries):
            next_row, next_skip = _report_draw_list_segment(page, section, entries,
                                                            entries_done, skip_lines)
            if (next_row, next_skip) == (entries_done, skip_lines):
                # Nothing fitted and nothing progressed: start a fresh page.
                page = _report_new_page(pages, meta, logo_draw)
                continue
            entries_done, skip_lines = next_row, next_skip
            if entries_done < len(entries):
                page = _report_new_page(pages, meta, logo_draw)

    # A section that exactly filled the previous page leaves the page opened for
    # it empty; drop those so the report never ends on a blank sheet.
    pages = [page for page in pages if not page['empty']]
    total_pages = len(pages)
    streams = []
    for index, page in enumerate(pages, start=1):
        page['text'].append(f'{REPORT_MUTED} rg')
        page['text'].append(_pdf_right_text(REPORT_RIGHT, REPORT_FOOTER_Y, f'Page {index} of {total_pages}', size=7.5))
        page['text'].append(f'{REPORT_INK} rg')
        page['text'].append('ET')
        streams.append('\n'.join(page['draw'] + page['text']).encode('latin-1', 'replace'))
    return _pdf_objects(streams, image_object=image_object)

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
    issuer_vat_number = (_doc_value(settings, 'vat_number', '') or '').strip()
    issuer_company_reg_no = (_doc_value(settings, 'company_reg_no', '') or '').strip()
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
    customer_is_company = _doc_value(document, 'customer_type', '') == 'company'
    customer_vat_number = (custom_fields.get('vat_number') or '').strip() if customer_is_company else ''
    customer_company_reg_no = (custom_fields.get('company_reg_no') or '').strip() if customer_is_company else ''
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
        *[line for line in [
            f'VAT No: {issuer_vat_number}' if issuer_vat_number else '',
            f'Company Reg No: {issuer_company_reg_no}' if issuer_company_reg_no else '',
        ] if line],
        *[line for line in issuer_address if line],
    ], size=8.2, leading=10, max_lines=9)

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
    if customer_vat_number:
        customer_lines.append(f'VAT No: {customer_vat_number}')
    if customer_company_reg_no:
        customer_lines.append(f'Company Reg No: {customer_company_reg_no}')
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
    # Move the table up under the address blocks. Page 1 takes as many rows as
    # genuinely fit above the summary/banking floor (instead of the earlier
    # hard six-row cap that left a large blank area and jumped to page 2). Any
    # remaining rows continue on following pages - each with the same column
    # headings and a page number - before the summary/banking block. Every item
    # is always printed
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
        draw.append(_pdf_rect(INVOICE_TABLE_X, header_y - 5, INVOICE_TABLE_RIGHT_EDGE - INVOICE_TABLE_X, 18, fill='0 0 0'))
        text.append('1 1 1 rg')
        _add_pdf_lines(text, INVOICE_TABLE_X, header_y, ['PRODUCT'], size=7.5)
        _add_pdf_lines(text, QTY_COLUMN_X, header_y, ['QTY'], size=7.5)
        _add_pdf_lines(text, DAYS_COLUMN_X, header_y, ['DAYS'], size=7.5)
        _add_pdf_lines(text, RATE_COLUMN_X, header_y, ['RATE'], size=7.5)
        _add_pdf_lines(text, SUBTOTAL_COLUMN_X, header_y, ['SUBTOTAL'], size=7.5)
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
            product_lines = _wrap_pdf_cell_text(name, max_chars=42, max_lines=3)
            if sku and len(product_lines) < 3:
                product_lines.append(str(sku)[:42])
            _add_pdf_lines(text, INVOICE_TABLE_X, y_pos, product_lines, size=8, leading=10, max_lines=3)
            _add_pdf_lines(text, QTY_COLUMN_X, y_pos, [str(item['quantity'])], size=8)
            _add_pdf_lines(text, DAYS_COLUMN_X, y_pos, [days_text], size=8)
            _add_pdf_lines(text, RATE_COLUMN_X, y_pos, [f"R{line_view['unit_excl']:.2f}"], size=8)
            _add_pdf_lines(text, SUBTOTAL_COLUMN_X, y_pos, [f"R{line_view['subtotal_excl']:.2f}"], size=8)
            _add_pdf_lines(text, TAX_COLUMN_X, y_pos, [f"R{line_view['tax']:.2f}"], size=8)
            _add_pdf_lines(text, TOTAL_INCL_COLUMN_X, y_pos, [f"R{line_view['total_incl']:.2f}"], size=8)
            y_pos -= 43
        return y_pos

    # Fill page 1 by measured capacity instead of leaving two rows' worth of
    # blank space after row 6. Continuation pages still use the same capacity
    # calculation and repeated headings.
    continuation_table_y = 675
    summary_floor_y = 58 + ((len(totals) - 1) * 14)
    # Lowest y the LAST item row of a page may reach: below it the summary rows
    # (14pt apart), their 12pt gap and the row pitch itself would not fit.
    last_row_y_floor = summary_floor_y + 12 + 43

    def rows_that_fit(first_row_y):
        return max(1, int((first_row_y - last_row_y_floor) // 43) + 1)

    first_page_limit = rows_that_fit(table_y - 24)
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
    # Banking details belong on the left, aligned with the document/table edge,
    # while totals sit on the right. Keeping the two blocks side by side avoids
    # the old behaviour where 5-8 line invoices used page 1 for rows/totals but
    # pushed only the banking details onto page 2.
    bank_y = totals_y
    bank_detail_lines = bank_lines[1:]
    bank_last_y = bank_y - 13 - ((len(bank_detail_lines) - 1) * 13 if bank_detail_lines else 0)
    if bank_last_y < 58:
        text_commands.append('ET')
        streams.append('\n'.join(draw_commands + text_commands).encode('latin-1', 'replace'))
        bank_draw_commands = [logo_draw_command] if logo_draw_command else []
        bank_text_commands = ['BT']
        bank_text_commands.append(_pdf_text_command(455, 760, f'{display_label} {display_number}', size=8.5, font='F2'))
        bank_text_commands.append(_pdf_text_command(455, 746, f'Page {len(streams) + 1}', size=8.5))
        bank_text_commands.append(_pdf_text_command(INVOICE_TABLE_X, 715, 'Thank you for your business.', size=8.8, font='F2'))
        bank_text_commands.append(_pdf_text_command(INVOICE_TABLE_X, 690, 'Banking details', size=8.5, font='F2'))
        _add_pdf_lines(bank_text_commands, INVOICE_TABLE_X, 675, bank_detail_lines, size=8.5, leading=13, max_lines=7)
        bank_text_commands.append('ET')
        streams.append('\n'.join(bank_draw_commands + bank_text_commands).encode('latin-1', 'replace'))
        return _pdf_objects(streams, image_object=image_object)
    text_commands.append(_pdf_text_command(INVOICE_TABLE_X, bank_y + 18, 'Thank you for your business.', size=8.8, font='F2'))
    text_commands.append(_pdf_text_command(INVOICE_TABLE_X, bank_y, 'Banking details', size=8.5, font='F2'))
    _add_pdf_lines(text_commands, INVOICE_TABLE_X, bank_y - 13, bank_detail_lines, size=8.5, leading=13, max_lines=7)
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
    issuer_vat_number = (_doc_value(settings, 'vat_number', '') or '').strip()
    issuer_company_reg_no = (_doc_value(settings, 'company_reg_no', '') or '').strip()
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
    if issuer_vat_number:
        lines.append(f'Issuer VAT No: {issuer_vat_number}')
    if issuer_company_reg_no:
        lines.append(f'Issuer Company Reg No: {issuer_company_reg_no}')
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
    custom_fields = custom_fields_for(document)
    customer_is_company = _doc_value(document, 'customer_type', '') == 'company'
    customer_vat_number = (custom_fields.get('vat_number') or '').strip() if customer_is_company else ''
    customer_company_reg_no = (custom_fields.get('company_reg_no') or '').strip() if customer_is_company else ''
    if customer_vat_number:
        lines.append(f'Customer VAT No: {customer_vat_number}')
    if customer_company_reg_no:
        lines.append(f'Customer Company Reg No: {customer_company_reg_no}')
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


# --- branch portal QR sheet (A4, one code per sheet) --------------------------
#
# The sheet the counter prints and displays so a customer can register their
# trailer details from their own phone. Don chose ONE code per sheet (not a grid
# of codes), so this is a poster: the branch, the code, and the address in plain
# text underneath for anyone whose camera will not cooperate.
#
# The code is drawn as VECTOR squares straight from the QR module matrix rather
# than embedding the PNG: a bitmap blown up to poster size goes soft, and a soft
# code is a code that does not scan. Horizontal runs of dark modules are merged
# into one rectangle each, so a version-4 code costs a few hundred operators
# instead of a few thousand, and the file stays small enough to open on a phone.
QR_SHEET_MARGIN = 48
QR_SHEET_QR_WIDTH = 320
QR_SHEET_PLATE_PAD = 16
QR_SHEET_PLATE_TOP = 676
QR_SHEET_INK = '0.10 0.13 0.18'
QR_SHEET_MUTED = '0.36 0.42 0.51'
QR_SHEET_PLATE_FILL = '1 1 1'
QR_SHEET_PLATE_STROKE = '0.82 0.86 0.90'
# The same sentence the on-screen print sheet uses (templates/admin/portal_print.html), so the
# PDF and the browser printout cannot describe the same code two different ways.
QR_SHEET_INSTRUCTION = 'Scan to register your details before you hire.'


def _qr_run_rects(matrix, left, top, module_size):
    """Each row's runs of dark modules, as one filled rectangle per run."""
    commands = []
    for row_index, row in enumerate(matrix):
        column = 0
        width = len(row)
        while column < width:
            if not row[column]:
                column += 1
                continue
            start = column
            while column < width and row[column]:
                column += 1
            commands.append(
                _pdf_rect(
                    left + start * module_size,
                    top - (row_index + 1) * module_size,
                    (column - start) * module_size,
                    module_size,
                    fill='0 0 0',
                )
            )
    return commands


def qr_sheet_pdf_bytes(branch_name, url, matrix, company_name=None, address=None, instruction=None):
    """One A4 page: the branch's portal code, big enough to scan from a counter.

    ``matrix`` is the QR module matrix (:func:`app.services.portal.qr_matrix`) so
    this module never has to know how the code was built.
    """
    plate_size = QR_SHEET_QR_WIDTH + (QR_SHEET_PLATE_PAD * 2)
    plate_left = (A4_PORTRAIT_WIDTH - plate_size) / 2
    plate_bottom = QR_SHEET_PLATE_TOP - plate_size
    module_size = QR_SHEET_QR_WIDTH / len(matrix)

    commands = [
        _pdf_rect(
            plate_left,
            plate_bottom,
            plate_size,
            plate_size,
            fill=QR_SHEET_PLATE_FILL,
            stroke=QR_SHEET_PLATE_STROKE,
            line_width=0.8,
        )
    ]
    # The white plate goes down before the modules: a code printed on tinted paper
    # is readable to a human and invisible to a scanner.
    commands.extend(_qr_run_rects(matrix, plate_left + QR_SHEET_PLATE_PAD, QR_SHEET_PLATE_TOP - QR_SHEET_PLATE_PAD, module_size))

    text = []
    if company_name:
        text.append(_pdf_centre_text(772, str(company_name).upper(), size=10, font='F2'))
    text.append(_pdf_centre_text(736, _pdf_fit(branch_name, 26, A4_PORTRAIT_WIDTH - (QR_SHEET_MARGIN * 2), bold=True), size=26, font='F2'))
    if address:
        text.append(_pdf_centre_text(716, _pdf_fit(address, 10, A4_PORTRAIT_WIDTH - (QR_SHEET_MARGIN * 2)), size=10))
    text.append(_pdf_centre_text(684 if address else 700, _pdf_fit(instruction or QR_SHEET_INSTRUCTION, 12, A4_PORTRAIT_WIDTH - (QR_SHEET_MARGIN * 2), bold=True), size=12, font='F2'))

    text.append(_pdf_centre_text(plate_bottom - 36, _pdf_fit(url, 12, A4_PORTRAIT_WIDTH - (QR_SHEET_MARGIN * 2), bold=True), size=12, font='F2'))
    text.append(_pdf_centre_text(plate_bottom - 56, 'or type this address into your browser', size=9))
    text.append(_pdf_centre_text(64, 'Print this sheet and display it where customers can see it.', size=9))

    commands.append('BT')
    commands.extend(text)
    commands.append('ET')
    return _pdf_objects('\n'.join(commands).encode('latin-1', 'replace'))


# --- POPIA document pack (A4, feature Q / phase 14, §Q1) ----------------------
#
# The printable pack: a cover page (Sano Trailers logo, "POPIA compliance pack",
# the generated date and an acceptance certificate table) followed by each
# document's full text, every page footed with document name + version + page
# number. A single-document sheet carries the same certificate for just that
# document and, for the operator agreement, a signature block.
#
# Built with the same in-house primitives as the day report and QR sheet
# (_pdf_rect / _pdf_text_command / _pdf_centre_text / _pdf_wrap / _pdf_objects),
# so no new dependency is introduced.

import re as _re

PACK_LEFT = 48
PACK_RIGHT = A4_PORTRAIT_WIDTH - 48
PACK_WIDTH = PACK_RIGHT - PACK_LEFT
PACK_TOP = 800
PACK_BOTTOM = 58
PACK_FOOTER_Y = 40
PACK_INK = '0.10 0.13 0.18'
PACK_MUTED = '0.36 0.42 0.51'
PACK_ACCENT = '0.08 0.39 1'
PACK_HEADER_FILL = '0.13 0.17 0.24'
PACK_ROW_FILL = '0.965 0.976 0.992'
PACK_RULE = '0.855 0.886 0.925'


def _pack_logo_centred(width=120, top_y=790):
    """The Sano logo centred at the top of the cover page, or (None, []) without one."""
    logo_bytes = _document_logo_bytes()
    if not logo_bytes:
        return None, []
    logo_width, logo_height = _jpeg_dimensions(logo_bytes)
    display_height = width * logo_height / logo_width
    left = (A4_PORTRAIT_WIDTH - width) / 2
    draw = [f'q {width:.2f} 0 0 {display_height:.2f} {left:.2f} {top_y - display_height:.2f} cm /Im1 Do Q']
    image_object = (
        f'<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} '
        f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\n'
    ).encode() + b'stream\n' + logo_bytes + b'\nendstream'
    return image_object, draw


def _pack_strip_inline(text):
    """Markdown inline syntax -> plain text for the single-byte PDF font."""
    text = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = _re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = _re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    return text.strip()


def _pack_md_items(text):
    """Markdown -> a list of (text, size, bold, indent, gap_before) items for the PDF."""
    items = []
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        heading = _re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(0).split()[0])
            size = {1: 13.5, 2: 12, 3: 11}.get(level, 10.5)
            items.append((_pack_strip_inline(heading.group(1)), size, True, 0, 10))
            continue
        if _re.fullmatch(r"([-*_])\1{2,}", stripped):
            items.append(("", 0, False, 0, 6))
            continue
        if stripped.startswith(">"):
            items.append((_pack_strip_inline(stripped.lstrip(">").strip()), 9.5, False, 12, 2))
            continue
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(_re.fullmatch(r"[-: ]+", c or "-") for c in cells):
                continue
            items.append(("  |  ".join(cells), 8.5, False, 0, 2))
            continue
        item = _re.match(r"^([-*+]|\d+[.)])\s+(.*)$", stripped)
        if item:
            items.append(("• " + _pack_strip_inline(item.group(2)), 9.5, False, 14, 2))
            continue
        items.append((_pack_strip_inline(stripped), 9.5, False, 0, 2))
    return items


def _pack_signature_block():
    """The operator-agreement signature block (the document is a contract)."""
    return [
        ("Signature block", 12, True, 0, 20),
        ("Adopted electronically in the app, which records the document identity, version and "
         "hash, and the person accepting, as evidence of the parties' agreement (ECTA 25 of "
         "2002; POPIA s21(1)).", 9, False, 0, 6),
        ("Signed for Sano Trailers (responsible party):", 9.5, True, 0, 14),
        ("Name: ________________________________", 9.5, False, 0, 4),
        ("Date: ________________________________", 9.5, False, 0, 4),
        ("Signed for the Operator:", 9.5, True, 0, 14),
        ("Name: ________________________________", 9.5, False, 0, 4),
        ("Date: ________________________________", 9.5, False, 0, 4),
    ]


def _pack_layout(items):
    """Word-wrap ``items`` and lay them onto pages; one page = a list of
    ``(text, size, bold, indent, y)`` lines. No footers here."""
    physical = []  # (text, size, bold, indent, gap_before)
    for (text, size, bold, indent, gap) in items:
        if not text:
            physical.append(("", size, bold, indent, gap))
            continue
        wrapped = _pdf_wrap(text, size, PACK_WIDTH - indent, bold=bold, max_lines=None)
        for j, wline in enumerate(wrapped):
            physical.append((wline, size, bold, indent, gap if j == 0 else 0))
    pages = []
    page_lines = []
    y = PACK_TOP
    for (text, size, bold, indent, gap) in physical:
        leading = (size + 4) if text else 0
        if text and y - gap - leading < PACK_BOTTOM and page_lines:
            pages.append(page_lines)
            page_lines = []
            y = PACK_TOP
        y -= gap
        if text:
            page_lines.append((text, size, bold, indent, y))
            y -= leading
    if page_lines:
        pages.append(page_lines)
    return pages


def _pack_render_page(lines, footer_text):
    """Render one laid-out page to a latin-1 content stream with a footer."""
    text_cmds = ["BT"]
    for (text, size, bold, indent, y) in lines:
        text_cmds.append(
            _pdf_text_command(PACK_LEFT + indent, y, text, size=size, font="F2" if bold else "F1")
        )
    text_cmds.append(f"{PACK_MUTED} rg")
    text_cmds.append(_pdf_text_command(PACK_LEFT, PACK_FOOTER_Y, footer_text, size=8))
    text_cmds.append(f"{PACK_INK} rg")
    text_cmds.append("ET")
    return "\n".join(text_cmds).encode("latin-1", "replace")


def _pack_render_pages(pages, footer_fn):
    """Render laid-out pages; ``footer_fn(page_number, total)`` writes each footer."""
    total = len(pages)
    return [_pack_render_page(page, footer_fn(index, total)) for index, page in enumerate(pages, start=1)]


def _pack_certificate(commands, entries, top_y):
    """Draw the "Acceptance certificate" table and return the y below it.

    ``entries`` is a list of dicts with ``title``, ``version``, ``hash_prefix``,
    ``accepted_by`` and ``accepted_at``.
    """
    commands.append(f"{PACK_ACCENT} rg")
    commands.append(_pdf_text_command(PACK_LEFT, top_y, "Acceptance certificate", size=13, font="F2"))
    commands.append(f"{PACK_INK} rg")
    columns = [
        ("Document", PACK_LEFT, 205),
        ("Version", PACK_LEFT + 205, 55),
        ("Hash", PACK_LEFT + 260, 90),
        ("Accepted by", PACK_LEFT + 350, 105),
        ("Date", PACK_LEFT + 455, 92),
    ]
    row_height = 18
    header_y = top_y - 22
    commands.append(_pdf_rect(PACK_LEFT, header_y - row_height + 3, PACK_WIDTH, row_height, fill=PACK_HEADER_FILL))
    commands.append("1 1 1 rg")
    for label, x, _width in columns:
        commands.append(_pdf_text_command(x + 4, header_y - 3, label, size=8, font="F2"))
    commands.append(f"{PACK_INK} rg")
    y = header_y - row_height - 2
    for index, entry in enumerate(entries):
        if index % 2 == 1:
            commands.append(_pdf_rect(PACK_LEFT, y - row_height + 2, PACK_WIDTH, row_height - 1, fill=PACK_ROW_FILL))
        date_text = display_local_date(entry.get("accepted_at")) if entry.get("accepted_at") else "—"
        values = [
            _pdf_fit(entry.get("title") or "—", 8.5, columns[0][2] - 8),
            entry.get("version") or "—",
            entry.get("hash_prefix") or "—",
            _pdf_fit(entry.get("accepted_by") or "—", 8.5, columns[3][2] - 8),
            date_text,
        ]
        for (_label, x, _width), value in zip(columns, values):
            commands.append(_pdf_text_command(x + 4, y, value, size=8.5))
        commands.append(_pdf_rect(PACK_LEFT, y - row_height + 2, PACK_WIDTH, 0.6, fill=PACK_RULE))
        y -= row_height
    return y - 8


def _pack_cover_page(entries, logo_draw):
    """One page: logo, title, generated date and the acceptance certificate table."""
    commands = list(logo_draw or [])
    commands.append("BT")
    title_y = 706 if logo_draw else 782
    commands.append(_pdf_centre_text(title_y, "POPIA compliance pack", size=22, font="F2"))
    commands.append(_pdf_centre_text(title_y - 26, "Sano Trailers", size=13))
    commands.append(f"{PACK_MUTED} rg")
    commands.append(_pdf_centre_text(title_y - 44, f"Generated {display_local_date(local_now_iso())}", size=10))
    commands.append(f"{PACK_INK} rg")
    _pack_certificate(commands, entries, title_y - 72)
    commands.append(f"{PACK_MUTED} rg")
    commands.append(_pdf_centre_text(PACK_FOOTER_Y + 10, "POPIA compliance pack", size=8))
    commands.append(f"{PACK_INK} rg")
    commands.append("ET")
    return "\n".join(commands).encode("latin-1", "replace")


def _pack_footer_label(entry, version):
    """The footer label: document name (and version) trimmed to the page width."""
    label = f"{entry['title']} · v{version}" if version else entry["title"]
    return _pdf_fit(label, 9, PACK_WIDTH)


def popia_pack_pdf_bytes():
    """The full printable pack: cover + certificate table + every document, footered."""
    from app.services import popia_pack

    statuses = {entry["key"]: entry for entry in popia_pack.pack_status()}
    image_object, logo_draw = _pack_logo_centred()
    cover_entries = []
    for entry in popia_pack.PACK:
        key = entry["key"]
        status = statuses[key]
        cover_entries.append({
            "title": entry["title"],
            "version": popia_pack.document_version(key),
            "hash_prefix": popia_pack.document_hash(key)[:12],
            "accepted_by": status.get("accepted_by"),
            "accepted_at": status.get("accepted_at"),
        })
    streams = [_pack_cover_page(cover_entries, logo_draw)]
    for entry in popia_pack.PACK:
        key = entry["key"]
        version = popia_pack.document_version(key)
        label = _pack_footer_label(entry, version)
        items = [(entry["title"], 14, True, 0, 0)]
        items.append((f"Version {version}" if version else "Version —", 10, False, 0, 2))
        items.append(("", 0, False, 0, 6))
        items.extend(_pack_md_items(popia_pack.document_text(key)))
        pages = _pack_layout(items)

        def footer(n, total, label=label):
            return f"{label} · Page {n} of {total}"

        streams.extend(_pack_render_pages(pages, footer))
    return _pdf_objects(streams, image_object=image_object)


def popia_document_pdf_bytes(key):
    """One document's A4 sheet: title, its acceptance certificate, the text, and
    the signature block when the manifest says the document requires one."""
    from app.services import popia_pack

    entry = popia_pack.MANIFEST[key]
    statuses = {item["key"]: item for item in popia_pack.pack_status()}
    status = statuses[key]
    version = popia_pack.document_version(key)
    hash_prefix = popia_pack.document_hash(key)[:12]
    label = _pack_footer_label(entry, version)
    certificate = [{
        "title": entry["title"],
        "version": version,
        "hash_prefix": hash_prefix,
        "accepted_by": status.get("accepted_by"),
        "accepted_at": status.get("accepted_at"),
    }]

    items = _pack_md_items(popia_pack.document_text(key))
    if entry["signature_required"]:
        items.extend(_pack_signature_block())
    body_pages = _pack_layout(items)
    total = 1 + len(body_pages)

    commands = ["BT"]
    top_y = 786
    commands.append(_pdf_centre_text(top_y, "POPIA compliance pack", size=11, font="F2"))
    commands.append(f"{PACK_MUTED} rg")
    commands.append(_pdf_centre_text(top_y - 16, _pdf_fit(entry["title"], 10, A4_PORTRAIT_WIDTH - 96), size=10))
    commands.append(f"{PACK_INK} rg")
    _pack_certificate(commands, certificate, top_y - 34)
    commands.append(f"{PACK_MUTED} rg")
    commands.append(_pdf_text_command(PACK_LEFT, PACK_FOOTER_Y, f"{label} · Page 1 of {total}", size=8))
    commands.append(f"{PACK_INK} rg")
    commands.append("ET")
    streams = ["\n".join(commands).encode("latin-1", "replace")]

    def footer(n, _body_total, label=label):
        # ``n`` is 1-based within the body; page 1 is the certificate page.
        return f"{label} · Page {n + 1} of {total}"

    streams.extend(_pack_render_pages(body_pages, footer))
    return _pdf_objects(streams)
