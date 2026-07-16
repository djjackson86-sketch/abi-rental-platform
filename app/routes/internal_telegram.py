from flask import Blueprint, current_app, jsonify, request

from app.services.telegram import send_daily_summary, send_test_message

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
