from datetime import datetime

from app.db import get_db, now
from app.services.settings import get_company_settings
from app.services.timezone import display_local_date, display_local_datetime, parse_iso_datetime
from app.services.numbering import next_in_sequence
from app.services.orders import get_order, order_items, rental_days

DOCUMENT_TYPES = {
    "quote": {"label": "Quote", "prefix": "QUO"},
    "contract": {"label": "Contract", "prefix": "CON"},
    "invoice": {"label": "Invoice", "prefix": "INV"},
    "packing_slip": {"label": "Packing slip", "prefix": "PCK"},
}


def document_type_options():
    return DOCUMENT_TYPES


def _next_document_number(document_type):
    """Next quote/invoice number, continuing the client's Booqable sequence."""
    prefix = DOCUMENT_TYPES[document_type]["prefix"]
    rows = get_db().execute(
        "SELECT number FROM documents WHERE document_type = ? AND number != ''",
        (document_type,),
    ).fetchall()
    return next_in_sequence([row["number"] for row in rows], prefix)


def create_document(order_id, document_type):
    if document_type not in DOCUMENT_TYPES:
        raise ValueError("Unsupported document type")
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    db = get_db()
    revision_of_id = None
    revision_number = 0
    number = '' if document_type == 'invoice' else _next_document_number(document_type)
    if document_type == 'invoice':
        existing = db.execute("SELECT id, status FROM documents WHERE order_id = ? AND document_type = 'invoice' ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        if existing:
            if existing["status"] == "draft":
                raise ValueError("A proforma invoice already exists for this order")
            raise ValueError("A finalized invoice already exists for this order")
    cur = db.execute(
        """INSERT INTO documents (order_id, document_type, status, number, pdf_path, revision_of_id, revision_number, revised_at, created_at)
        VALUES (?, ?, 'draft', ?, '', ?, ?, ?, ?)""",
        (order_id, document_type, number, revision_of_id, revision_number, now() if revision_of_id else '', now()),
    )
    db.commit()
    return cur.lastrowid


def is_proforma_invoice(document):
    return bool(document) and document['document_type'] == 'invoice' and document['status'] == 'draft' and not (document['number'] or '').strip()


def display_document_label(document):
    if is_proforma_invoice(document):
        return 'Proforma Invoice'
    return label_for(document['document_type'])


def display_document_number(document):
    if is_proforma_invoice(document):
        return ''
    return (document['number'] or '').strip() or 'Unnumbered'


def finalize_document(document_id):
    db = get_db()
    document = get_document(document_id)
    if not document:
        raise ValueError('Document not found')
    if document['status'] == 'finalized':
        return document_id
    if document['document_type'] == 'invoice':
        existing = db.execute(
            "SELECT id FROM documents WHERE order_id = ? AND document_type = 'invoice' AND status = 'finalized' AND id != ? LIMIT 1",
            (document['order_id'], document_id),
        ).fetchone()
        if existing:
            raise ValueError('A finalized invoice already exists for this order')
    number = (document['number'] or '').strip()
    if document['document_type'] == 'invoice' and not number:
        number = _next_document_number('invoice')
    db.execute(
        "UPDATE documents SET status = 'finalized', number = ? WHERE id = ?",
        (number, document_id),
    )
    db.commit()
    return document_id


def list_documents(query="", document_type="", status="", start_date="", end_date=""):
    sql = """SELECT d.*, o.order_number, o.total, o.deposit_option, c.name AS customer_name
        FROM documents d
        LEFT JOIN orders o ON o.id = d.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE 1=1"""
    params = []
    if query:
        sql += """ AND (LOWER(d.number) LIKE ? OR LOWER(o.order_number) LIKE ? OR LOWER(c.name) LIKE ?)"""
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    if document_type:
        sql += " AND d.document_type = ?"
        params.append(document_type)
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    if start_date:
        sql += " AND DATE(d.created_at) >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND DATE(d.created_at) <= ?"
        params.append(end_date)
    sql += " ORDER BY d.created_at DESC, d.id DESC"
    return get_db().execute(sql, params).fetchall()


def document_filter_counts():
    db = get_db()
    type_rows = db.execute("SELECT document_type, COUNT(*) count FROM documents GROUP BY document_type").fetchall()
    status_rows = db.execute("SELECT status, COUNT(*) count FROM documents GROUP BY status").fetchall()
    return {
        "document_type": {row["document_type"]: row["count"] for row in type_rows},
        "status": {row["status"]: row["count"] for row in status_rows},
    }



def get_document(document_id):
    return get_db().execute(
        """SELECT d.*, o.order_number, o.customer_id, o.status AS order_status, o.payment_status, o.start_at, o.end_at,
            o.subtotal, o.discount_total, o.discount_mode, o.discount_value, o.tax_total, o.deposit_total, o.deposit_option, o.total, o.due_total, o.notes,
            COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id = o.id AND p.status = 'paid' AND COALESCE(p.deleted_at, '') = ''), 0) AS paid_total,
            c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone,
            c.address_line1 AS customer_address_line1, c.address_line2 AS customer_address_line2, c.suburb AS customer_suburb,
            c.city AS customer_city, c.province AS customer_province, c.postal_code AS customer_postal_code, c.country AS customer_country,
            c.custom_fields_json AS custom_fields_json,
            b.id AS branch_id, b.name AS branch_name, b.code AS branch_code, b.phone AS branch_phone, b.email AS branch_email,
            b.address_line1 AS branch_address_line1, b.address_line2 AS branch_address_line2, b.city AS branch_city,
            b.province AS branch_province, b.postal_code AS branch_postal_code,
            b.bank_name AS branch_bank_name, b.bank_account_name AS branch_bank_account_name,
            b.bank_account_number AS branch_bank_account_number, b.bank_branch_code AS branch_bank_branch_code,
            b.bank_account_type AS branch_bank_account_type, b.bank_reference_note AS branch_bank_reference_note
        FROM documents d
        LEFT JOIN orders o ON o.id = d.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches b ON b.id = o.collect_branch_id
        WHERE d.id = ?""",
        (document_id,),
    ).fetchone()


def documents_for_order(order_id):
    return get_db().execute(
        "SELECT * FROM documents WHERE order_id = ? ORDER BY created_at DESC, id DESC",
        (order_id,),
    ).fetchall()


def label_for(document_type):
    return DOCUMENT_TYPES.get(document_type, {}).get("label", document_type.replace("_", " ").title())


def _parse_document_datetime(value):
    try:
        return parse_iso_datetime(value)
    except ValueError:
        return None


def document_date(value):
    return display_local_date(value)


def document_datetime(value):
    return display_local_datetime(value)


def rental_days_for_document(document):
    start_at = _parse_document_datetime(document['start_at'])
    end_at = _parse_document_datetime(document['end_at'])
    return rental_days(start_at, end_at)


def rental_days_label(document):
    days = rental_days_for_document(document)
    return f"Rental days {days}"


def _row_get(row, key, default=None):
    """Read a column from a sqlite3.Row, a libsql Row or a dict alike."""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def document_tax_view(document, items):
    """VAT figures for an invoice or quote.

    Line items are always shown EXCLUDING VAT, with the VAT stated once in the
    summary (total without VAT / VAT / total with VAT). ``line_subtotal`` is the
    VAT-exclusive line amount in both tax modes; ``unit_price`` is only exclusive
    when prices exclude VAT, so it is converted when prices include VAT.

    ``rental_days`` is the number of days a RENTAL line was charged for, and is
    ``None`` for lines that are not rentals (sales, services, fixed-fee custom
    lines) so the document leaves that column blank rather than implying a hire.
    There is no per-line days column in ``order_items``: a rental line is priced
    as unit x qty x the order's rental days, so the order's day count IS the
    line's day count.
    """
    settings = get_company_settings()
    tax_mode = _row_get(settings, 'tax_mode', 'exclusive') or 'exclusive'
    order_days = rental_days_for_document(document)

    lines = []
    for item in items:
        unit = float(_row_get(item, 'unit_price', 0) or 0)
        subtotal = float(_row_get(item, 'line_subtotal', 0) or 0)
        tax = float(_row_get(item, 'line_tax', 0) or 0)
        if tax_mode == 'inclusive' and subtotal:
            unit = unit / (1 + (tax / subtotal))
        is_rental = (_row_get(item, 'product_type', '') == 'rental'
                     or _row_get(item, 'billing_mode', '') == 'rental_day')
        lines.append({
            'unit_excl': round(unit, 2),
            'subtotal_excl': round(subtotal, 2),
            'tax': round(tax, 2),
            'total_incl': round(subtotal + tax, 2),
            'rental_days': order_days if is_rental else None,
        })

    subtotal = round(float(_row_get(document, 'subtotal', 0) or 0), 2)
    discount = round(float(_row_get(document, 'discount_total', 0) or 0), 2)
    vat = round(float(_row_get(document, 'tax_total', 0) or 0), 2)
    net = round(subtotal - discount, 2)
    return {
        'lines': lines,
        'net': net,
        'discount': discount,
        'vat': vat,
        'gross': round(net + vat, 2),
        'rate': round(vat / subtotal * 100, 2) if subtotal else 0.0,
    }


def document_paid_stamp(document):
    """True when a settled invoice should print with a PAID stamp.

    Only invoices are stamped - a quote is never paid - and the decision reuses
    the order's own ``payment_status`` so the stamp always agrees with what the
    Orders screen shows. 'paid'/'overpaid' both mean nothing is outstanding.
    """
    if not document or _row_get(document, 'document_type', '') != 'invoice':
        return False
    return _row_get(document, 'payment_status', '') in ('paid', 'overpaid')


def printable_document(document_id):
    document = get_document(document_id)
    if not document:
        return None, []
    return document, order_items(document["order_id"])


def mark_document_email(document_id, sent_to, status, error=''):
    db = get_db()
    sent_at = now() if status == 'sent' else ''
    db.execute("UPDATE documents SET sent_at = ?, sent_to = ?, email_status = ?, email_error = ? WHERE id = ?", (sent_at, sent_to, status, error, document_id))
    db.commit()
