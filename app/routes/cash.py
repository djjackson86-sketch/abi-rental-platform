"""Cash-up / day-end routes for the dashboard (ticket ABI-341952951).

Small on purpose: every write is one form post from the dashboard's cash panel
and every read is a download of the same figures the panel shows.

Access model
------------
The whole blueprint is gated by the ``dashboard`` module (``("cash.",
"dashboard")`` in ``app.services.access._ENDPOINT_MODULE_RULES``), because the
panel lives on the dashboard and the client asked for "the users" to be able to
cash up.

The depot is **never trusted from the request**: it goes through
``cash.branch_for_request()``, which only accepts an id the session may already
reach and otherwise falls back to the depot the sign-in is acting as. A crafted
``?branch=`` or posted ``branch`` can therefore narrow a cash up, never widen
one — the same rule the branch filters on /orders, /calendar, /reports and
/inventory follow. The panel's own depot picker is a GET parameter named
``cash_branch`` (it belongs to the dashboard, not to a cash-up action), and every
write form carries the resolved depot in a hidden ``branch`` field.
"""
import csv
from io import StringIO

from flask import Blueprint, Response, flash, redirect, request, session, url_for

from app.routes.auth import login_required
from app.services import cash
from app.services.pdf_documents import report_pdf_bytes
from app.services.telegram import _send_document

bp = Blueprint("cash", __name__, url_prefix="/cash-up")


def _day_from_request():
    """The business day being acted on (falls back to today on junk input)."""
    return cash.parse_business_day(request.values.get("day"))


def _user_id():
    try:
        return int(session.get("user_id") or 0) or None
    except (TypeError, ValueError):
        return None


def _target_branch():
    """The depot the submitted form applies to (can only narrow the session)."""
    return cash.branch_for_request(request.form.get("branch", ""))


def _dashboard_url(branch_id=None):
    """Back to the dashboard, still showing the depot that was acted on."""
    if branch_id:
        return url_for("admin.dashboard", cash_branch=branch_id)
    return url_for("admin.dashboard")


def _done(message, branch_id=None):
    flash(message, "success")
    return redirect(_dashboard_url(branch_id))


@bp.post("")
@login_required
def save():
    """Cash up: record the cash counted in the drawer for the day."""
    day = _day_from_request()
    branch_id = _target_branch()
    try:
        cash.guard_writable_day(day)
        cash.save_cash_up(
            day,
            request.form.get("counted_cash", ""),
            request.form.get("notes", ""),
            branch_id=branch_id,
            user_id=_user_id(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(_dashboard_url(branch_id))
    return _done(f"Cash up saved for {day}", branch_id)


@bp.post("/notes")
@login_required
def save_notes():
    """Save just the end of day notes (no count needed)."""
    day = _day_from_request()
    branch_id = _target_branch()
    try:
        cash.guard_writable_day(day)
        cash.save_notes(day, request.form.get("notes", ""), branch_id=branch_id, user_id=_user_id())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(_dashboard_url(branch_id))
    return _done(f"End of day notes saved for {day}", branch_id)


@bp.post("/used")
@login_required
def add_used():
    """Add one cash-used line, with what the cash was used for."""
    day = _day_from_request()
    branch_id = _target_branch()
    try:
        cash.guard_writable_day(day)
        cash.add_cash_used(
            day,
            request.form.get("amount", ""),
            request.form.get("description", ""),
            branch_id=branch_id,
            user_id=_user_id(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(_dashboard_url(branch_id))
    return _done(f"Cash used added for {day}", branch_id)


@bp.post("/used/<int:entry_id>/delete")
@login_required
def delete_used(entry_id):
    day = _day_from_request()
    branch_id = _target_branch()
    try:
        cash.guard_writable_day(day)
        # Only ever the acting depot's own lines: the check is inside the service.
        cash.delete_cash_used(entry_id, branch_id=branch_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(_dashboard_url(branch_id))
    return _done(f"Cash used line removed for {day}", branch_id)


@bp.post("/bank")
@login_required
def add_bank_drop():
    """Record cash taken out of the drawer and dropped off at the bank.

    Amount only — the client asked for just the amount, and the line reduces the
    cash expected in the drawer exactly like cash used does.
    """
    day = _day_from_request()
    branch_id = _target_branch()
    try:
        cash.guard_writable_day(day)
        cash.add_bank_drop(
            day,
            request.form.get("amount", ""),
            branch_id=branch_id,
            user_id=_user_id(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(_dashboard_url(branch_id))
    return _done(f"Bank drop off added for {day}", branch_id)


@bp.post("/bank/<int:entry_id>/delete")
@login_required
def delete_bank_drop(entry_id):
    day = _day_from_request()
    branch_id = _target_branch()
    try:
        cash.guard_writable_day(day)
        # Only ever the acting depot's own lines: the check is inside the service.
        cash.delete_bank_drop(entry_id, branch_id=branch_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(_dashboard_url(branch_id))
    return _done(f"Bank drop off line removed for {day}", branch_id)


def _report():
    return cash.day_report(
        day=_day_from_request(),
        branch_id=cash.branch_for_request(request.args.get("branch", "")),
    )


def _report_for_branch(branch_id):
    return cash.day_report(day=_day_from_request(), branch_id=branch_id)


def _report_pdf_bytes(report):
    return report_pdf_bytes(cash.day_report_pdf_cards(
        report,
        user_name=session.get("user_name") or "",
        user_role=session.get("user_role") or "",
    ))


@bp.get("/report.pdf")
@login_required
def report_pdf():
    """Download the day's dashboard report (cards + cash up + notes).

    The figures come from the same ``day_report_rows`` the CSV export reads; the
    layout is a branded one-pager (logo, cards, "Prepared by: <user>").
    """
    report = _report()
    day = report["cash"]["day"]
    return Response(
        _report_pdf_bytes(report),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=dashboard-report-{day}.pdf"},
    )


@bp.post("/report/telegram")
@login_required
def submit_report_telegram():
    """Send the same dashboard day-report PDF to the configured Telegram chat."""
    branch_id = _target_branch()
    report = _report_for_branch(branch_id)
    cash_summary = report["cash"]
    day = cash_summary["day"]
    branch_name = cash_summary["branch_name"] or "this depot"
    filename = f"dashboard-report-{day}.pdf"
    caption = f"Day report — {branch_name} — {day}"
    result = _send_document(_report_pdf_bytes(report), filename, caption=caption)
    if result.get("sent"):
        flash(f"Day report sent on Telegram for {branch_name} ({day})", "success")
    elif result.get("skipped") == "disabled":
        flash("Telegram notifications are disabled; day report was not sent", "error")
    elif result.get("skipped") == "not_configured":
        flash("Telegram is not configured; day report was not sent", "error")
    else:
        flash("Day report could not be sent on Telegram", "error")
    return redirect(_dashboard_url(branch_id))


@bp.get("/export.csv")
@login_required
def export_csv():
    """Download the same report as CSV, with every cash-used line."""
    report = _report()
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
