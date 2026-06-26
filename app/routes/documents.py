from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
import csv
from io import StringIO

from app.routes.auth import login_required
from app.services.documents import create_document, label_for, list_documents, printable_document, document_filter_counts
from app.services.settings import get_company_settings

bp = Blueprint("documents", __name__, url_prefix="/documents")

@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    document_type = request.args.get("document_type", "")
    status = request.args.get("status", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    documents = list_documents(query=query, document_type=document_type, status=status, start_date=start_date, end_date=end_date)
    # Compute totals for metrics
    docs_total = len(documents)
    docs_total_amount = sum((d["total"] or 0) for d in documents)
    return render_template(
        "admin/documents/index.html",
        settings=get_company_settings(),
        documents=documents,
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
    return render_template(
        "admin/documents/detail.html",
        settings=get_company_settings(),
        document=document,
        items=items,
        label=label_for(document["document_type"]),
    )


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