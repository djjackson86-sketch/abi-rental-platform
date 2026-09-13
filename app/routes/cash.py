"""Cash-up / day-end routes for the dashboard (ticket ABI-341952951).

Small on purpose: every write is one form post from the dashboard's cash panel
and every read is a download of the same figures the panel shows.

Access model: the whole blueprint is gated by the ``dashboard`` module
(``("cash.", "dashboard")`` in ``app.services.access._ENDPOINT_MODULE_RULES``),
because the panel lives on the dashboard and the client asked for "the users" to
be able to cash up. The depot is never taken from the request — it always comes
from ``cash.acting_branch_id()``, which is the depot the session already acts as.
"""
import csv
from io import StringIO

from flask import Blueprint, Response, flash, redirect, request, session, url_for

from app.routes.auth import login_required
from app.services import cash
from app.services.pdf_documents import report_pdf_bytes

bp = Blueprint("cash", __name__, url_prefix="/cash-up")


def _day_from_request():
    """The business day being acted on (falls back to today on junk input)."""
    return cash.parse_business_day(request.values.get("day"))


def _user_id():
    try:
        return int(session.get("user_id") or 0) or None
    except (TypeError, ValueError):
        return None


def _done(message):
    flash(message, "success")
    return redirect(url_for("admin.dashboard"))


@bp.post("")
@login_required
def save():
    """Cash up: record the cash counted in the drawer for the day."""
    day = _day_from_request()
    try:
        cash.guard_writable_day(day)
        cash.save_cash_up(
            day,
            request.form.get("counted_cash", ""),
            request.form.get("notes", ""),
            user_id=_user_id(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.dashboard"))
    return _done(f"Cash up saved for {day}")


@bp.post("/notes")
@login_required
def save_notes():
    """Save just the end of day notes (no count needed)."""
    day = _day_from_request()
    try:
        cash.guard_writable_day(day)
        cash.save_notes(day, request.form.get("notes", ""), user_id=_user_id())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.dashboard"))
    return _done(f"End of day notes saved for {day}")


@bp.post("/used")
@login_required
def add_used():
    """Add one cash-used line, with what the cash was used for."""
    day = _day_from_request()
    try:
        cash.guard_writable_day(day)
        cash.add_cash_used(
            day,
            request.form.get("amount", ""),
            request.form.get("description", ""),
            user_id=_user_id(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.dashboard"))
    return _done(f"Cash used added for {day}")


@bp.post("/used/<int:entry_id>/delete")
@login_required
def delete_used(entry_id):
    day = _day_from_request()
    try:
        cash.guard_writable_day(day)
        # Only ever the acting depot's own lines: the check is inside the service.
        cash.delete_cash_used(entry_id, branch_id=cash.acting_branch_id())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.dashboard"))
    return _done(f"Cash used line removed for {day}")


@bp.get("/report.pdf")
@login_required
def report_pdf():
    """Download the day's dashboard report (figures + cash up + notes)."""
    report = cash.day_report(day=_day_from_request())
    day = report["cash"]["day"]
    return Response(
        report_pdf_bytes(cash.day_report_pdf_lines(report)),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=dashboard-report-{day}.pdf"},
    )


@bp.get("/export.csv")
@login_required
def export_csv():
    """Download the same report as CSV, with every cash-used line."""
    report = cash.day_report(day=_day_from_request())
    cash_summary = report["cash"]
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "item", "value"])
    writer.writerow(["Report", "Company", report["company"]])
    writer.writerow(["Report", "Depot", cash_summary["branch_name"]])
    writer.writerow(["Report", "Business day", cash_summary["day"]])
    writer.writerow(["Report", "Generated", report["generated_at"]])
    for row in cash.day_report_csv_rows(report):
        writer.writerow(row)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=dashboard-report-{cash_summary['day']}.csv"},
    )
