from app.services.documents import label_for, printable_document


def _escape_pdf_text(text):
    return str(text or '').replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _simple_pdf(lines):
    y = 800
    stream_lines = ['BT', '/F1 12 Tf']
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
    objects.append(b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>')
    objects.append(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')
    objects.append(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
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


def document_pdf_bytes(document_id):
    document, items = printable_document(document_id)
    if not document:
        raise ValueError('Document not found')
    label = label_for(document['document_type'])
    lines = [f'{label} {document["number"]}', f'Order: {document["order_number"]}', f'Customer: {document["customer_name"] or "-"}', f'Email: {document["customer_email"] or "-"}', f'Pickup: {document["start_at"] or "-"}', f'Return: {document["end_at"] or "-"}', '']
    for item in items:
        lines.append(f'{item["product_name"] or item["custom_name"]} x {item["quantity"]} @ R{float(item["unit_price"] or 0):.2f} = R{float(item["line_total"] or 0):.2f}')
    lines.extend(['', f'Subtotal: R{float(document["subtotal"] or 0):.2f}', f'Tax: R{float(document["tax_total"] or 0):.2f}', f'Security deposit: R{float(document["deposit_total"] or 0):.2f}', f'Total: R{float(document["total"] or 0):.2f}'])
    return _simple_pdf(lines)


def document_pdf_filename(document):
    prefix = label_for(document['document_type']).upper().replace(' ', '-')
    return f'{prefix}-{document["number"]}.pdf'
