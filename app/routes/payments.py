from flask import Blueprint, flash, redirect, render_template, url_for

from app.routes.auth import login_required
from app.services.payments import label_for, list_payments
from app.services.settings import get_company_settings

bp = Blueprint("payments", __name__, url_prefix="/payments")


@bp.route("")
@login_required
def index():
    return render_template("admin/payments/index.html", settings=get_company_settings(), payments=list_payments(), label_for=label_for)
