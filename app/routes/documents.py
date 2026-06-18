from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.services.documents import create_document, label_for, list_documents, printable_document
from app.services.settings import get_company_settings

bp = Blueprint("documents", __name__, url_prefix="/documents")


@bp.route("")
@login_required
def index():
    return render_template("admin/documents/index.html", settings=get_company_settings(), documents=list_documents(), label_for=label_for)


@bp.route("/<int:document_id>")
@login_required
def detail(document_id):
    document, items = printable_document(document_id)
    if not document:
        flash("Document not found", "error")
        return redirect(url_for("documents.index"))
    return render_template("admin/documents/detail.html", settings=get_company_settings(), document=document, items=items, label=label_for(document["document_type"]))


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
