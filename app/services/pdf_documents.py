from pathlib import Path

from flask import current_app

from app.services.documents import document_date, label_for, printable_document, rental_days_label
from app.services.customers import custom_fields_for
from app.services.settings import get_company_settings


DOCUMENT_LOGO_STATIC_PATH = 'img/sano-trailers-logo.jpg'
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


def _pdf_light_blue_rect(x, y, width, height):
    return f'q 0.86 0.94 1 rg {x:.2f} {y:.2f} {width:.2f} {height:.2f} re f Q'


def _add_pdf_lines(commands, x, y, lines, size: int | float = 9, leading=14, max_lines=None, font='F1'):
    for index, line in enumerate([line for line in lines if line]):
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
        display_width = 86
        display_height = display_width * logo_height / logo_width
        # The source logo image has built-in white padding. Shift the image left so
        # the visible logo artwork aligns with the issuer/address wording below.
        draw_commands.append(f'q {display_width:.2f} 0 0 {display_height:.2f} 25 {A4_PORTRAIT_HEIGHT - 42 - display_height:.2f} cm /Im1 Do Q')
        image_object = (
            f'<< /Type /XObject /Subtype /Image /Width {logo_width} /Height {logo_height} '
            f'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(logo_bytes)} >>\n'
        ).encode() + b'stream\n' + logo_bytes + b'\nendstream'

    text_commands = ['BT']
    # Top-left brand/address, matching the supplied template.
    _add_pdf_lines(text_commands, 36, 715, [
        issuer_name,
        *[line for line in [issuer_phone, issuer_email] if line],
        *[line for line in issuer_address if line],
    ], size=8.2, leading=12, max_lines=7)

    # Top-right invoice and order stack.
    detail_x = 430
    _add_pdf_lines(text_commands, detail_x, 760, [
        'Invoice',
        document['number'],
        f'Invoice date: {document_date(document["created_at"])}',
    ], size=8.5, leading=14)

    _add_pdf_lines(text_commands, detail_x, 625, [
        'Order',
        f'Order: {document["order_number"]}',
        f'Pickup: {document_date(document["start_at"])}',
        f'Return: {document_date(document["end_at"])}',
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
    if custom_fields.get('vehicle_make'):
        customer_lines.append(f'Vehicle Make: {custom_fields["vehicle_make"]}')
    if custom_fields.get('vehicle_color'):
        customer_lines.append(f'Vehicle Color: {custom_fields["vehicle_color"]}')
    if custom_fields.get('vehicle_reg_no'):
        customer_lines.append(f'Veh Reg No: {custom_fields["vehicle_reg_no"]}')
    if custom_fields.get('alternative_contact_name'):
        customer_lines.append(f'Alternative Contact Name: {custom_fields["alternative_contact_name"]}')
    if custom_fields.get('alternative_contact_number'):
        customer_lines.append(f'Alternative Contact Number: {custom_fields["alternative_contact_number"]}')
    if custom_fields.get('alternative_contact_relationship'):
        customer_lines.append(f'Alternative Contact Relationship: {custom_fields["alternative_contact_relationship"]}')
    # Bill To block, aligned under the logo/brand on the left.
    _add_pdf_lines(text_commands, 36, 625, customer_lines, size=8.5, leading=13, max_lines=16)

    # Invoice table and totals.
    table_y = 430
    draw_commands.append(_pdf_light_blue_rect(36, table_y - 5, 523, 18))
    _add_pdf_lines(text_commands, 36, table_y, ['Item'], size=7.5)
    _add_pdf_lines(text_commands, 220, table_y, ['Qty'], size=7.5)
    _add_pdf_lines(text_commands, 270, table_y, ['Unit'], size=7.5)
    _add_pdf_lines(text_commands, 335, table_y, ['Subtotal'], size=7.5)
    _add_pdf_lines(text_commands, 415, table_y, ['Tax'], size=7.5)
    _add_pdf_lines(text_commands, 510, table_y, ['Total'], size=7.5)
    y = table_y - 24
    for item in items[:8]:
        name = item['product_name'] or item['custom_name'] or 'Item'
        sku = item['product_sku'] or ''
        _add_pdf_lines(text_commands, 36, y, [name, sku], size=8, leading=11, max_lines=2)
        _add_pdf_lines(text_commands, 220, y, [str(item['quantity'])], size=8)
        _add_pdf_lines(text_commands, 270, y, [f'R{float(item["unit_price"] or 0):.2f}'], size=8)
        _add_pdf_lines(text_commands, 335, y, [f'R{float(item["line_subtotal"] or 0):.2f}'], size=8)
        _add_pdf_lines(text_commands, 415, y, [f'R{float(item["line_tax"] or 0):.2f}'], size=8)
        _add_pdf_lines(text_commands, 510, y, [f'R{float(item["line_total"] or 0):.2f}'], size=8)
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
    totals = [
        ('Subtotal', f'R{float(document["subtotal"] or 0):.2f}'),
        ('Tax', f'R{float(document["tax_total"] or 0):.2f}'),
        ('Security deposit', f'R{float(document["deposit_total"] or 0):.2f}'),
        ('Total', f'R{float(document["total"] or 0):.2f}'),
        ('Paid', f'R{float(document["paid_total"] or 0):.2f}'),
        ('Amount due', f'R{float(document["due_total"] or 0):.2f}'),
    ]
    draw_commands.append(_pdf_light_blue_rect(382, totals_y - ((len(totals) - 1) * 14) - 5, 177, (len(totals) * 14) + 4))
    for index, (label, amount) in enumerate(totals):
        line_y = totals_y - (index * 14)
        if label == 'Total':
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
    _add_pdf_lines(text_commands, 36, bank_y, bank_lines, size=8.5, leading=13, max_lines=8)
    text_commands.append('ET')
    stream = '\n'.join(draw_commands + text_commands).encode('latin-1', 'replace')
    return _pdf_objects(stream, image_object=image_object)


def document_pdf_bytes(document_id):
    document, items = printable_document(document_id)
    if not document:
        raise ValueError('Document not found')
    label = label_for(document['document_type'])
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
    lines = [f'{label} {document["number"]}']
    if document['document_type'] == 'invoice':
        lines.append(f'Invoice date: {document["created_at"] or "-"}')
    lines.append(f'Issuer: {issuer_name}')
    if issuer_email:
        lines.append(f'Issuer email: {issuer_email}')
    if issuer_phone:
        lines.append(f'Issuer phone: {issuer_phone}')
    lines.extend([line for line in issuer_address if line])
    lines.extend([f'Order: {document["order_number"]}', f'Pickup: {document["start_at"] or "-"}', f'Return: {document["end_at"] or "-"}'])
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
    lines.extend(['', f'Subtotal: R{float(document["subtotal"] or 0):.2f}', f'Tax: R{float(document["tax_total"] or 0):.2f}', f'Security deposit: R{float(document["deposit_total"] or 0):.2f}', f'Total: R{float(document["total"] or 0):.2f}'])
    if document['document_type'] == 'invoice':
        lines.extend([f'Paid: R{float(document["paid_total"] or 0):.2f}', f'Amount due: R{float(document["due_total"] or 0):.2f}'])
    logo_bytes = _document_logo_bytes()
    if document['document_type'] == 'invoice':
        return _invoice_template_pdf(document, items, settings, logo_bytes=logo_bytes)
    return _simple_pdf(lines, logo_bytes=logo_bytes)


def document_pdf_filename(document):
    prefix = label_for(document['document_type']).upper().replace(' ', '-')
    return f'{prefix}-{document["number"]}.pdf'
