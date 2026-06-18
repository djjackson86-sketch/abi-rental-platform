from app.db import get_db, now
from app.services.orders import get_order, order_items

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


def list_documents():
    return get_db().execute(
        """SELECT d.*, o.order_number, o.total, c.name AS customer_name
        FROM documents d
        LEFT JOIN orders o ON o.id = d.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        ORDER BY d.created_at DESC, d.id DESC"""
    ).fetchall()


def get_document(document_id):
    return get_db().execute(
        """SELECT d.*, o.order_number, o.customer_id, o.status AS order_status, o.start_at, o.end_at,
            o.subtotal, o.tax_total, o.deposit_total, o.total, o.due_total, o.notes,
            c.name AS customer_name, c.email AS customer_email, c.phone AS customer_phone
        FROM documents d
        LEFT JOIN orders o ON o.id = d.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
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


def printable_document(document_id):
    document = get_document(document_id)
    if not document:
        return None, []
    return document, order_items(document["order_id"])
