from flask import Blueprint, current_app, jsonify, request

from app.services.telegram import send_all_branches_day_report, send_daily_summary, send_test_message

bp = Blueprint("internal_telegram", __name__, url_prefix="/api/internal/telegram")


def _authorized():
    expected = current_app.config.get("TELEGRAM_CRON_SECRET", "")
    supplied = request.headers.get("x-cron-secret", "")
    return bool(expected) and supplied == expected


@bp.post("/test")
def telegram_test():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    result = send_test_message()
    return jsonify(result), 200 if result.get("ok") else 502


@bp.post("/daily-summary")
def telegram_daily_summary():
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    result = send_daily_summary(request.args.get("date") or None)
    return jsonify(result), 200 if result.get("ok") else 502


@bp.post("/all-branches-report")
def telegram_all_branches_report():
    """Retry / sweep the combined "All branches" day report (ticket ABI-341953055).

    The same guarded call the depots' own submissions make, so it is safe to run
    on a schedule: it sends only when every active depot has submitted for the
    day, only once per business day, and only when the day's report has not been
    delivered yet (which is also the retry after a Telegram failure).

    ``?date=YYYY-MM-DD`` looks at another business day; the default is today.
    Returns 200 with a ``skipped`` reason for every quiet outcome — a partial day
    is a normal state, not an error — and 502 only when Telegram refused a send
    that was actually attempted.
    """
    if not _authorized():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    result = send_all_branches_day_report(request.args.get("date") or None)
    return jsonify(result), 200 if result.get("ok") else 502
