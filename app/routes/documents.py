from flask import Blueprint, Response, current_app, flash, make_response, redirect, render_template, request, url_for
import csv
from io import StringIO
from pathlib import Path

from app.routes.auth import login_required
from app.services.documents import (
    accept_quote,
    create_document,
    display_document_label,
    display_document_number,
    document_date,
    document_datetime,
    document_filter_counts,
    document_has_rental_items,
    document_paid_stamp,
    document_tax_view,
    finalize_document,
    get_document,
    label_for,
    list_documents,
    document_totals,
    mark_document_email,
    printable_document,
    rental_days_label,
)
from app.services.settings import get_company_settings
from app.services.email_delivery import build_invoice_email_subject, build_outlook_draft_eml, render_email_template
from app.services.pdf_documents import DOCUMENT_LOGO_STATIC_PATH, document_pdf_bytes, document_pdf_filename
from app.services.customers import custom_fields_for

# Email-sized signature logo (360px wide) so clients that ignore CSS sizing
# still show a sane logo instead of the full 1200px document artwork.
EMAIL_LOGO_STATIC_PATH = 'img/sano-trailers-email-logo.jpg'

bp = Blueprint("documents", __name__, url_prefix="/documents")

EMAIL_DRAFT_DOCUMENT_TYPES = {'invoice', 'quote'}
PAGE_SIZE = 25


def _display_limit():
    try:
        requested = int(request.args.get("limit") or PAGE_SIZE)
    except (TypeError, ValueError):
        requested = PAGE_SIZE
    return max(PAGE_SIZE, min(requested, 5000))


def _prevent_generated_document_cache(response):
    """Documents are rendered from the current order every request.

    Keep browsers/proxies from reusing an older invoice/quote/PDF after the
    order's lines have been edited under the same document URL.
    """
    response.headers['Cache-Control'] = 'no-store, no-cache, max-age=0, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def _email_context(document, settings, label=None, number=None):
    branch_email = document['branch_email'] or settings['email']
    branch_contact = document['branch_phone'] or settings['phone']
    return {
        'customer_name': document['customer_name'] or 'Customer',
        'document_label': label or display_document_label(document),
        'document_number': number or display_document_number(document),
        'order_number': document['order_number'],
        'company_name': settings['company_name'],
        'branch_name': document['branch_name'] or settings['company_name'],
        'branch_email': branch_email,
        'branch_contact': branch_contact,
        'branch_phone': branch_contact,
    }


def _invoice_email_logo_bytes(settings):
    if not settings['invoice_email_signature_include_logo']:
        return None
    static_folder = Path(current_app.static_folder)
    # Use the email-sized logo (360px wide, ~15 KB) for the signature. The full
    # 1200px document logo stays for the PDF; sending it inline made the
    # signature huge in clients that ignore CSS (Outlook's Word engine) and
    # bloated every draft by ~110 KB.
    for relative_path in (EMAIL_LOGO_STATIC_PATH, DOCUMENT_LOGO_STATIC_PATH):
        logo_path = static_folder / relative_path
        if logo_path.exists():
            return logo_path.read_bytes()
    return None


def _prepare_email_draft(document_id, to_email, message=None):
    document = get_document(document_id)
    if not document:
        raise ValueError("Document not found")
    if document['document_type'] not in EMAIL_DRAFT_DOCUMENT_TYPES:
        raise ValueError("Email draft is available for invoices and quotes only")
    to_email = (to_email or document["customer_email"] or "").strip()
    if not to_email:
        raise ValueError("Customer email is required before preparing the email")
    settings = get_company_settings()
    if not settings:
        raise ValueError("Company settings not found")
    label = display_document_label(document)
    number = display_document_number(document)
    subject = build_invoice_email_subject(label, number, document['order_number'])
    context = _email_context(document, settings, label=label, number=number)
    body = message or render_email_template(settings['invoice_email_message'], context)
    signature = render_email_template(settings['invoice_email_signature'], context)
    pdf_bytes = document_pdf_bytes(document_id)
    pdf_filename = document_pdf_filename(document)
    eml_bytes = build_outlook_draft_eml(
        to_email,
        subject,
        body,
        pdf_bytes,
        pdf_filename,
        cc_email=(settings['email'] or '').strip() or 'info@sanotrailers.co.za',
        signature=signature,
        logo_bytes=_invoice_email_logo_bytes(settings),
    )
    return document, eml_bytes, f"EMAIL-{pdf_filename.rsplit('.', 1)[0]}.eml"

@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    document_type = request.args.get("document_type", "")
    status = request.args.get("status", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    display_limit = _display_limit()
    documents = list_documents(query=query, document_type=document_type, status=status, start_date=start_date, end_date=end_date, limit=display_limit)
    totals = document_totals(query=query, document_type=document_type, status=status, start_date=start_date, end_date=end_date)
    docs_total = totals["total"]
    docs_total_amount = totals["total_amount"]
    return render_template(
        "admin/documents/index.html",
        settings=get_company_settings(),
        documents=documents,
        display_limit=display_limit,
        next_limit=display_limit + PAGE_SIZE,
        label_for=label_for,
        filter_counts=document_filter_counts(),
        docs_total=docs_total,
        docs_total_amount=docs_total_amount,
        filters={"query": query, "document_type": document_type, "status": status, "start_date": start_date, "end_date": end_date},
    )


@bp.route("/export.csv")
@login_required
def export_csv():
    query = request.args.get("query", "").strip()
    document_type = request.args.get("document_type", "")
    status = request.args.get("status", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    documents = list_documents(query=query, document_type=document_type, status=status, start_date=start_date, end_date=end_date)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["number", "document_type", "order_number", "customer_name", "status", "total", "created_at"])
    for d in documents:
        writer.writerow([
            d["number"],
            d["document_type"],
            d["order_number"],
            d["customer_name"],
            d["status"],
            d["total"],
            d["created_at"],
        ])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=documents.csv"})


@bp.route("/<int:document_id>")
@login_required
def detail(document_id):
    document, items = printable_document(document_id)
    if not document:
        flash("Document not found", "error")
        return redirect(url_for("documents.index"))
    settings = get_company_settings()
    if not settings:
        flash("Company settings not found", "error")
        return redirect(url_for("documents.index"))
    email_message = render_email_template(
        settings['invoice_email_message'],
        _email_context(document, settings),
    )
    email_signature = render_email_template(settings['invoice_email_signature'], _email_context(document, settings))
    has_rental_items = document_has_rental_items(items)
    return _prevent_generated_document_cache(make_response(render_template(
        "admin/documents/detail.html",
        settings=settings,
        document=document,
        items=items,
        has_rental_items=has_rental_items,
        tax_view=document_tax_view(document, items),
        is_paid=document_paid_stamp(document),
        label=label_for(document["document_type"]),
        display_label=display_document_label(document),
        display_number=display_document_number(document),
        custom_fields=custom_fields_for(document),
        document_date=document_date,
        document_datetime=document_datetime,
        rental_days_label=rental_days_label(document),
        email_message=email_message,
        email_signature=email_signature,
        email_signature_include_logo=bool(settings['invoice_email_signature_include_logo']),
    )))


@bp.route("/<int:document_id>/download.pdf")
@login_required
def download_pdf(document_id):
    document = get_document(document_id)
    if not document:
        flash("Document not found", "error")
        return redirect(url_for("documents.index"))
    pdf_bytes = document_pdf_bytes(document_id)
    return _prevent_generated_document_cache(Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={document_pdf_filename(document)}"},
    ))


@bp.post("/orders/<int:order_id>")
@login_required
def create_for_order(order_id):
    try:
        document_id = create_document(order_id, request.form.get("document_type", ""))
        flash("Document created", "success")
        return redirect(url_for("documents.detail", document_id=document_id))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:document_id>/accept-quote")
@login_required
def accept_quote_route(document_id):
    try:
        accept_quote(document_id)
        flash("Quote accepted", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("documents.detail", document_id=document_id))


@bp.post("/<int:document_id>/finalize")
@login_required
def finalize(document_id):
    try:
        finalize_document(document_id)
        flash("Invoice finalized and numbered", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("documents.detail", document_id=document_id))


@bp.post("/<int:document_id>/send-email")
@login_required
def send_document_email(document_id):
    try:
        document, eml_bytes, eml_name = _prepare_email_draft(
            document_id,
            request.form.get("to_email"),
            request.form.get("message"),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.detail", document_id=document_id))
    mark_document_email(document_id, request.form.get("to_email") or document["customer_email"] or "", "prepared")
    return Response(
        eml_bytes,
        mimetype="message/rfc822",
        headers={"Content-Disposition": f"attachment; filename={eml_name}"},
    )
