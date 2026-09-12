import math
from pathlib import Path

from flask import current_app

from app.services.documents import display_document_label, display_document_number, document_date, document_datetime, document_paid_stamp, document_tax_view, label_for, printable_document, rental_days_label
from app.services.customers import custom_fields_for
from app.services.settings import get_company_settings


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


def _escape_pdf_text(text):
    return str(text or '').replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


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
    y = 680 if logo_bytes else 800
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
    stream_lines = content_lines + ['BT', '/F1 12 Tf']
    for line in lines:
        stream_lines.append(f'50 {y} Td ({_escape_pdf_text(line)}) Tj')
        stream_lines.append(f'-50 -18 Td')
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
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>')
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


def _pdf_objects(stream, image_object=None):
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    ]
    resources = b'/Font << /F1 4 0 R /F2 6 0 R >>'
    if image_object:
        resources += b' /XObject << /Im1 7 0 R >>'
    objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox ' + A4_PORTRAIT_MEDIABOX.encode() + b' /Resources << ' + resources + b' >> /Contents 5 0 R >>')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>')
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
        draw_commands.append(f'q {display_width:.2f} 0 0 {display_height:.2f} {LOGO_IMAGE_X} {logo_bottom:.2f} cm /Im1 Do Q')
        image_object = (
            f'<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\n'
        ).encode() + b'stream\n' + logo_bytes + b'\nendstream'

    # A settled invoice is stamped PAID. Drawn first so every real figure sits
    # on top of it, and pinned in the empty band above the totals.
    if document_paid_stamp(document):
        draw_commands.extend(_pdf_paid_stamp(395, 498))

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
    _add_pdf_lines(text_commands, detail_x, 611, [
        f'Order: {document["order_number"]}',
        f'Pickup: {document_datetime(document["start_at"])}',
        f'Return: {document_datetime(document["end_at"])}',
        rent_label,
    ], size=8.5, leading=14)

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
    table_y = min(430, customer_bottom_y - 26)
    draw_commands.append(_pdf_light_blue_rect(36, table_y - 5, 523, 18))
    # Line items are quoted EXCLUDING VAT; the VAT is stated once in the summary.
    tax_view = document_tax_view(document, items)
    # Column headings as the client specified them (PRODUCT | QTY | DAYS | UNIT
    # EXCL. VAT | SUBTOTAL EXCL. VAT | TAX | TOTAL INCL. VAT). Widths at 7.5pt:
    # unit 58, subtotal 80, tax 15, total-incl 62 - the last column ends near
    # x=502, inside the 559 table edge, so nothing collides.
    _add_pdf_lines(text_commands, 36, table_y, ['PRODUCT'], size=7.5)
    _add_pdf_lines(text_commands, 165, table_y, ['QTY'], size=7.5)
    _add_pdf_lines(text_commands, 200, table_y, ['DAYS'], size=7.5)
    _add_pdf_lines(text_commands, 235, table_y, ['UNIT EXCL. VAT'], size=7.5)
    _add_pdf_lines(text_commands, 310, table_y, ['SUBTOTAL EXCL. VAT'], size=7.5)
    _add_pdf_lines(text_commands, 405, table_y, ['TAX'], size=7.5)
    _add_pdf_lines(text_commands, 440, table_y, ['TOTAL INCL. VAT'], size=7.5)
    y = table_y - 24
    for index, item in enumerate(items[:8]):
        name = item['product_name'] or item['custom_name'] or 'Item'
        sku = item['product_sku'] or ''
        line_view = tax_view['lines'][index] if index < len(tax_view['lines']) else {
            'unit_excl': 0.0, 'subtotal_excl': 0.0, 'tax': 0.0, 'total_incl': 0.0, 'rental_days': None}
        days_text = str(line_view.get('rental_days')) if line_view.get('rental_days') else '-'
        _add_pdf_lines(text_commands, 36, y, [name, sku], size=8, leading=11, max_lines=2)
        _add_pdf_lines(text_commands, 165, y, [str(item['quantity'])], size=8)
        _add_pdf_lines(text_commands, 200, y, [days_text], size=8)
        _add_pdf_lines(text_commands, 235, y, [f"R{line_view['unit_excl']:.2f}"], size=8)
        _add_pdf_lines(text_commands, 310, y, [f"R{line_view['subtotal_excl']:.2f}"], size=8)
        _add_pdf_lines(text_commands, 405, y, [f"R{line_view['tax']:.2f}"], size=8)
        _add_pdf_lines(text_commands, 440, y, [f"R{line_view['total_incl']:.2f}"], size=8)
        y -= 36

    totals_y = max(170, y - 12)
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
    # Summary: total without VAT, the VAT itself, then the total with VAT.
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
    totals.extend([
        ('Paid', f'R{float(document["paid_total"] or 0):.2f}'),
        ('Amount due', f'R{float(document["due_total"] or 0):.2f}'),
    ])
    draw_commands.append(_pdf_light_blue_rect(382, totals_y - ((len(totals) - 1) * 14) - 5, 177, (len(totals) * 14) + 4))
    for index, (label, amount) in enumerate(totals):
        line_y = totals_y - (index * 14)
        if label == 'Total with VAT':
            draw_commands.append(_pdf_light_blue_rect(386, line_y - 5, 169, 16))
            text_commands.append('0.08 0.39 1 rg')
            text_commands.append(_pdf_text_command(390, line_y, label, size=8.8, font='F2'))
            text_commands.append(_pdf_text_command(505, line_y, amount, size=8.8, font='F2'))
            text_commands.append('0 0 0 rg')
        else:
            text_commands.append(_pdf_text_command(390, line_y, label, size=8.8))
            text_commands.append(_pdf_text_command(505, line_y, amount, size=8.8))
    bank_y = totals_y - (len(totals) * 14) - 26
    text_commands.append(_pdf_text_command(36, bank_y + 18, 'Thank you for your business.', size=8.8, font='F2'))
    text_commands.append(_pdf_text_command(36, bank_y, 'Banking details', size=8.5, font='F2'))
    _add_pdf_lines(text_commands, 36, bank_y - 13, bank_lines[1:], size=8.5, leading=13, max_lines=7)
    text_commands.append('ET')
    stream = '\n'.join(draw_commands + text_commands).encode('latin-1', 'replace')
    return _pdf_objects(stream, image_object=image_object)


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
    lines.extend([f'Order: {document["order_number"]}', f'Pickup: {document_datetime(document["start_at"])}', f'Return: {document_datetime(document["end_at"])}'])
    if document['document_type'] == 'invoice':
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
        lines.extend([f'Paid: R{float(document["paid_total"] or 0):.2f}', f'Amount due: R{float(document["due_total"] or 0):.2f}'])
    logo_bytes = _document_logo_bytes()
    if document['document_type'] in {'invoice', 'quote'}:
        return _invoice_template_pdf(document, items, settings, logo_bytes=logo_bytes)
    return _simple_pdf(lines, logo_bytes=logo_bytes)


def document_pdf_filename(document):
    prefix = label_for(document['document_type']).upper().replace(' ', '-')
    number = (document['number'] or '').strip() or 'PROFORMA'
    return f'{prefix}-{number}.pdf'
