from datetime import datetime

from app.db import get_db, now
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
    prefix = DOCUMENT_TYPES[document_type]["prefix"]
    row = get_db().execute("SELECT COUNT(*) c FROM documents WHERE document_type = ?", (document_type,)).fetchone()
    return f"{prefix}-{(row['c'] or 0) + 1:05d}"


def create_document(order_id, document_type):
    if document_type not in DOCUMENT_TYPES:
        raise ValueError("Unsupported document type")
    order = get_order(order_id)
    if not order:
        raise ValueError("Order not found")
    db = get_db()
    number = _next_document_number(document_type)
    cur = db.execute(
        """INSERT INTO documents (order_id, document_type, status, number, pdf_path, created_at)
        VALUES (?, ?, 'draft', ?, '', ?)""",
        (order_id, document_type, number, now()),
    )
    db.commit()
    return cur.lastrowid


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
        """SELECT d.*, o.order_number, o.customer_id, o.status AS order_status, o.start_at, o.end_at,
            o.subtotal, o.tax_total, o.deposit_total, o.deposit_option, o.total, o.due_total, o.notes,
            COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id = o.id AND p.status = 'paid'), 0) AS paid_total,
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
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None


def document_date(value):
    parsed = _parse_document_datetime(value)
    if parsed:
        return parsed.date().isoformat()
    text = str(value or '').strip()
    if not text:
        return '—'
    return text[:10]


def rental_days_for_document(document):
    start_at = _parse_document_datetime(document['start_at'])
    end_at = _parse_document_datetime(document['end_at'])
    return rental_days(start_at, end_at)


def rental_days_label(document):
    days = rental_days_for_document(document)
    return f"{days} {'Day' if days == 1 else 'Days'} Rental"


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
