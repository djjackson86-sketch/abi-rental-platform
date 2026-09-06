from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.routes.auth import login_required
from app.services.payments import archive_payment, display_payment_date, get_payment, label_for, list_payments, update_payment
from app.services.settings import get_company_settings

bp = Blueprint("payments", __name__, url_prefix="/payments")


@bp.route("")
@login_required
def index():
    include_archived = request.args.get("status") == "archived"
    return render_template(
        "admin/payments/index.html",
        settings=get_company_settings(),
        payments=list_payments(include_archived=include_archived),
        label_for=label_for,
        display_payment_date=display_payment_date,
        include_archived=include_archived,
    )


@bp.route("/<int:payment_id>/edit", methods=["GET", "POST"])
@login_required
def edit(payment_id):
    payment = get_payment(payment_id)
    if not payment or payment["deleted_at"]:
        flash("Payment not found", "error")
        return redirect(url_for("payments.index"))
    if request.method == "POST":
        try:
            update_payment(payment_id, request.form)
            flash("Payment updated", "success")
            return redirect(url_for("orders.detail", order_id=payment["order_id"]))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template(
        "admin/payments/edit.html",
        settings=get_company_settings(),
        payment=payment,
        payment_date=(payment["payment_date"] or payment["created_at"] or "")[:16],
    )


@bp.post("/<int:payment_id>/delete")
@login_required
def delete(payment_id):
    payment = get_payment(payment_id)
    try:
        archive_payment(payment_id)
        flash("Payment deleted", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    if payment and payment["order_id"]:
        return redirect(url_for("orders.detail", order_id=payment["order_id"]))
    return redirect(url_for("payments.index"))
