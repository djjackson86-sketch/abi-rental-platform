import html
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app

from app.db import get_db
from app.services.orders import get_order, order_items
from app.services.timezone import display_local_datetime


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def notifications_enabled():
    return _truthy(current_app.config.get("TELEGRAM_NOTIFICATIONS_ENABLED"))


def telegram_configured():
    return bool(current_app.config.get("TELEGRAM_BOT_TOKEN") and current_app.config.get("TELEGRAM_CHAT_ID"))


def daily_summary_enabled():
    """Whether the ``Sano Trailers daily summary`` text report may be sent.

    Off by default: ABI asked for that recurring text report to stop (ticket
    ABI-341953054), so ``send_daily_summary()`` no longer posts it. Only that one
    message is suppressed - new customer / new order notifications, the test
    message and the manual day-report PDF all still send. Setting
    ``TELEGRAM_DAILY_SUMMARY_ENABLED`` (env var, no code change needed) turns the
    report back on, which is also how the toggle itself is proven in tests.
    """
    return _truthy(current_app.config.get("TELEGRAM_DAILY_SUMMARY_ENABLED"))


def all_branches_report_enabled():
    """Whether the combined "All branches" day report may be sent.

    On by default — the automatic combined report is what ticket ABI-341953055
    asked for — and switchable from the environment
    (``TELEGRAM_ALL_BRANCHES_REPORT_ENABLED=0``) with no code change. A missing
    key also means on, so an app that never carries the setting still behaves as
    the client asked.
    """
    value = current_app.config.get("TELEGRAM_ALL_BRANCHES_REPORT_ENABLED")
    if value is None:
        return True
    return _truthy(value)


def all_branches_chat_id():
    """The chat the combined report goes to: its own override, else the group.

    Falls back to the existing ``TELEGRAM_CHAT_ID``, so the feature needs no new
    secret; ``TELEGRAM_ALL_BRANCHES_CHAT_ID`` is only there for a deployment that
    would rather keep the combined figures out of the depot group.
    """
    override = str(current_app.config.get("TELEGRAM_ALL_BRANCHES_CHAT_ID") or "").strip()
    return override or str(current_app.config.get("TELEGRAM_CHAT_ID") or "")


def all_branches_report_filename(day):
    """``All branches_Dashboard Report_<day>.pdf`` — the downloads' own helper.

    Imported here rather than duplicated: the route module owns the only
    filename sanitiser in the app, and two copies of it would eventually drift.
    """
    from app.routes.cash import _report_filename
    return _report_filename("All branches", day)


def send_all_branches_day_report(day=None):
    """Send the combined "All branches" day report once the day is complete.

    Ticket ABI-341953055: after every depot has submitted its own day report, the
    main user's dashboard report — the combined figures for the whole business —
    is posted to the Telegram group automatically. The same call is the retry
    path (the internal, cron-secret gated endpoint re-runs it), so every guard
    lives here rather than in the submit route:

    * the kill switch ``TELEGRAM_ALL_BRANCHES_REPORT_ENABLED`` and, underneath
      it, the global ``TELEGRAM_NOTIFICATIONS_ENABLED``;
    * **every active depot** must have submitted — a partial day sends nothing;
    * the day's combined report must not already have been delivered
      (``day_report_sends``), so a re-submit or a retry can never post twice.

    The report is built from the explicit list of active depots, so it is never
    narrowed by the (depot-scoped) session that triggered the last submission,
    and it is signed by the owner account, not by that staff member.

    Never raises — the caller is a staff-facing submit. Always returns
    ``{'ok', 'sent', 'day'}`` plus either ``skipped`` (with the reason) or the
    Telegram result; ``ok`` is False only when the send itself failed.
    """
    from app.services import cash
    from app.services.pdf_documents import report_pdf_bytes

    result = {"day": cash.parse_business_day(day), "sent": False}
    try:
        if not all_branches_report_enabled():
            return {**result, "ok": True, "skipped": "all_branches_disabled"}
        if not notifications_enabled():
            return {**result, "ok": True, "skipped": "disabled"}
        if not telegram_configured():
            return {**result, "ok": True, "skipped": "not_configured"}
        if not cash.all_branches_submitted(result["day"]):
            return {
                **result,
                "ok": True,
                "skipped": "awaiting_depots",
                "depots": len(cash.active_depot_ids()),
                "missing": cash.missing_day_report_depots(result["day"]),
            }
        if cash.day_report_sent(result["day"]) is not None:
            return {**result, "ok": True, "skipped": "already_sent"}

        depot_ids = cash.active_depot_ids()
        report = cash.day_report(
            day=result["day"], aggregate=True, branch_ids=depot_ids, all_depots=True,
        )
        owner_name, owner_role = cash.day_report_prepared_by()
        document = report_pdf_bytes(cash.day_report_pdf_cards(
            report, user_name=owner_name, user_role=owner_role,
        ))
        filename = all_branches_report_filename(result["day"])
        caption = f"All branches day report — {result['day']}"
        chat_id = all_branches_chat_id()
        send = _send_document(document, filename, caption=caption, chat_id=chat_id)
    except Exception as exc:  # a combined report must never break a depot's submit
        current_app.logger.exception("All branches day report failed: %s", exc)
        return {**result, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if send.get("sent"):
        # Only a delivered report closes the day: a failed send leaves no marker,
        # which is what lets the retry sweep try again.
        cash.record_day_report_send(result["day"], chat_id)
    return {
        **result,
        "ok": bool(send.get("ok")),
        "sent": bool(send.get("sent")),
        "day": result["day"],
        "depots": len(depot_ids),
        "filename": filename,
        "caption": caption,
        "chat_id": chat_id,
        "send": send,
    }


def _escape(value):
    return html.escape(str(value if value is not None else ""), quote=False)


def _money(value):
    symbol = current_app.config.get("CURRENCY_SYMBOL", "R")
    try:
        return f"{symbol}{float(value or 0):,.2f}"
    except Exception:
        return f"{symbol}0.00"


def _base_url():
    return (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")


def _order_admin_url(order):
    if not _base_url() or not order:
        return ""
    return f"{_base_url()}/orders/{order['id']}"


def _send_message(text):
    if not notifications_enabled():
        return {"ok": True, "sent": False, "skipped": "disabled"}
    if not telegram_configured():
        return {"ok": True, "sent": False, "skipped": "not_configured"}

    token = current_app.config["TELEGRAM_BOT_TOKEN"]
    chat_id = current_app.config["TELEGRAM_CHAT_ID"]
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", "replace")
            sent = 200 <= response.status < 300
            return {"ok": sent, "sent": sent, "status": response.status, "body": body[:500]}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        current_app.logger.warning("Telegram send failed: HTTP %s %s", exc.code, body)
        return {"ok": False, "sent": False, "status": exc.code, "body": body}
    except Exception as exc:
        current_app.logger.warning("Telegram send failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "sent": False, "error": f"{type(exc).__name__}: {exc}"}


def _send_document(document_bytes, filename, caption='', chat_id=None):
    """Send a PDF/document to the configured Telegram chat.

    Uses the same enable/config checks as text notifications. Kept small and
    stdlib-only so the cash dashboard can submit its existing day report PDF
    without adding dependencies or new secrets.

    ``chat_id`` overrides the configured chat for one send (used by the combined
    "All branches" report, which may target its own group); it defaults to the
    existing ``TELEGRAM_CHAT_ID``, so every current caller is unchanged.
    """
    if not notifications_enabled():
        return {"ok": True, "sent": False, "skipped": "disabled"}
    if not telegram_configured():
        return {"ok": True, "sent": False, "skipped": "not_configured"}

    boundary = f"----abi-telegram-{uuid.uuid4().hex}"

    def field(name, value):
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode("utf-8")

    body = bytearray()
    body.extend(field("chat_id", chat_id or current_app.config["TELEGRAM_CHAT_ID"]))
    if caption:
        body.extend(field("caption", caption))
    safe_filename = str(filename or "document.pdf").replace('"', '')
    body.extend((
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"document\"; filename=\"{safe_filename}\"\r\n"
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8"))
    body.extend(document_bytes or b"")
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    token = current_app.config["TELEGRAM_BOT_TOKEN"]
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=bytes(body),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8", "replace")
            sent = 200 <= response.status < 300
            return {"ok": sent, "sent": sent, "status": response.status, "body": response_body[:500]}
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", "replace")[:500]
        current_app.logger.warning("Telegram document send failed: HTTP %s %s", exc.code, response_body)
        return {"ok": False, "sent": False, "status": exc.code, "body": response_body}
    except Exception as exc:
        current_app.logger.warning("Telegram document send failed: %s: %s", type(exc).__name__, exc)
        return {"ok": False, "sent": False, "error": f"{type(exc).__name__}: {exc}"}


def _safe_send(builder):
    try:
        return _send_message(builder())
    except Exception as exc:
        current_app.logger.exception("Telegram notification failed: %s", exc)
        return {"ok": False, "sent": False, "error": str(exc)}


def _row_value(row, key, default=""):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def _branch_label(row, *keys):
    for key in keys:
        value = _row_value(row, key)
        if value:
            return value
    return "Unassigned / head office"


def format_customer_message(customer):
    return "\n".join([
        "👤 <b>New customer created</b>",
        f"Name: {_escape(customer['name'])}",
        f"Type: {_escape(customer['customer_type'])}",
        f"Created at branch: {_escape(_branch_label(customer, 'branch_name'))}",
        f"Email: {_escape(customer['email'] or 'Not supplied')}",
        f"Phone: {_escape(customer['phone'] or 'Not supplied')}",
        f"Marketing: {'Yes' if customer['marketing_opt_in'] else 'No'}",
    ])


def send_new_customer_notification(customer_id):
    def build():
        customer = get_db().execute(
            """SELECT c.*, b.name AS branch_name
            FROM customers c LEFT JOIN branches b ON b.id = c.branch_id
            WHERE c.id = ?""",
            (customer_id,),
        ).fetchone()
        if not customer:
            return f"👤 <b>New customer created</b>\nCustomer ID: {_escape(customer_id)}"
        return format_customer_message(customer)
    return _safe_send(build)


def format_order_message(order, items):
    lines = [
        "📦 <b>New order / booking request</b>",
        f"Order: {_escape(order['order_number'])}",
        f"Customer: {_escape(order['customer_name'] or 'Not supplied')}",
        f"Created at branch: {_escape(_branch_label(order, 'collect_branch_name'))}",
        f"Email: {_escape(order['customer_email'] or 'Not supplied')}",
        f"Phone: {_escape(order['customer_phone'] or 'Not supplied')}",
        f"Pickup: {_escape(display_local_datetime(order['start_at']) if order['start_at'] else 'Not set')}",
        f"Return: {_escape(display_local_datetime(order['end_at']) if order['end_at'] else 'Not set')}",
        "",
        "<b>Items</b>",
    ]
    for item in items:
        name = item["product_name"] or item["custom_name"] or "Custom item"
        lines.append(f"• {_escape(name)} × {_escape(item['quantity'])} — {_money(item['line_total'])}")
    lines.extend([
        "",
        f"Subtotal: {_money(order['subtotal'])}",
        f"Discount: {_money(order['discount_total'])}",
        f"Tax: {_money(order['tax_total'])}",
        f"Deposit: {_money(order['deposit_total'])}",
        f"Total due: {_money(order['due_total'])}",
        f"Status: {_escape(order['status'])} / {_escape(order['payment_status'])}",
    ])
    if order["coupon_code"]:
        lines.append(f"Coupon: {_escape(order['coupon_code'])}")
    url = _order_admin_url(order)
    if url:
        lines.append(f"Admin: {_escape(url)}")
    if order["notes"]:
        lines.append(f"Notes: {_escape(order['notes'])}")
    return "\n".join(lines)


def send_new_order_notification(order_id):
    def build():
        order = get_order(order_id)
        if not order:
            return f"📦 <b>New order / booking request</b>\nOrder ID: {_escape(order_id)}"
        return format_order_message(order, order_items(order_id))
    return _safe_send(build)


def _business_today():
    tz_name = current_app.config.get("BUSINESS_TIMEZONE", "Africa/Johannesburg")
    return datetime.now(ZoneInfo(tz_name)).date()


def _parse_date(value):
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return _business_today() + timedelta(days=1)


def daily_summary_counts(target_date):
    db = get_db()
    date_text = target_date.isoformat()
    order_branch_select = """o.*, c.name AS customer_name,
        cb.name AS collect_branch_name, rb.name AS return_branch_name"""
    order_branch_join = """LEFT JOIN customers c ON c.id = o.customer_id
        LEFT JOIN branches cb ON cb.id = o.collect_branch_id
        LEFT JOIN branches rb ON rb.id = o.return_branch_id"""
    going_out = db.execute(
        f"""SELECT {order_branch_select} FROM orders o {order_branch_join}
        WHERE DATE(o.start_at) = ? AND o.status != 'canceled' ORDER BY o.start_at, o.id""",
        (date_text,),
    ).fetchall()
    coming_back = db.execute(
        f"""SELECT {order_branch_select} FROM orders o {order_branch_join}
        WHERE DATE(o.end_at) = ? AND o.status != 'canceled' ORDER BY o.end_at, o.id""",
        (date_text,),
    ).fetchall()
    due_row = db.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(due_total),0) AS total FROM orders WHERE payment_status = 'payment_due' AND status != 'canceled'"
    ).fetchone()
    return {"date": date_text, "going_out": going_out, "coming_back": coming_back, "payment_due_count": due_row["count"] or 0, "payment_due_total": due_row["total"] or 0}


def format_daily_summary(summary):
    lines = [f"📋 <b>Sano Trailers daily summary for {_escape(summary['date'])}</b>", ""]
    lines.append(f"<b>Going out ({len(summary['going_out'])})</b>")
    if summary["going_out"]:
        for order in summary["going_out"][:20]:
            branch = _branch_label(order, 'collect_branch_name')
            lines.append(f"• {_escape(order['order_number'])} — {_escape(order['customer_name'] or 'No customer')} at {_escape(display_local_datetime(order['start_at']) if order['start_at'] else '')} — {_escape(branch)}")
    else:
        lines.append("• None")
    lines.append("")
    lines.append(f"<b>Coming back ({len(summary['coming_back'])})</b>")
    if summary["coming_back"]:
        for order in summary["coming_back"][:20]:
            branch = _branch_label(order, 'return_branch_name', 'collect_branch_name')
            lines.append(f"• {_escape(order['order_number'])} — {_escape(order['customer_name'] or 'No customer')} at {_escape(display_local_datetime(order['end_at']) if order['end_at'] else '')} — {_escape(branch)}")
    else:
        lines.append("• None")
    lines.append("")
    lines.append(f"Payment due orders: {_escape(summary['payment_due_count'])} totaling {_money(summary['payment_due_total'])}")
    return "\n".join(lines)


def send_daily_summary(date_text=None):
    """Send (or, by default, deliberately NOT send) the daily summary text report.

    Ticket ABI-341953054: the client asked for the ``Sano Trailers daily summary``
    Telegram text report to stop. The report is now suppressed here, in the one
    place that posts it, so it can never go out - including from the existing
    external cron that calls ``POST /api/internal/telegram/daily-summary``.

    The call still succeeds (``ok`` True, ``sent`` False) and still carries the
    ``date`` and ``sections`` metadata, so any cron keeps getting a healthy 200
    with no loud failure, while nothing at all is posted. Wording is preserved in
    ``format_daily_summary()`` for when the report is switched back on.
    """
    target_date = _parse_date(date_text)
    summary = daily_summary_counts(target_date)
    if not notifications_enabled():
        # Unchanged, pre-existing behaviour: the global switch wins and says so.
        result = {"ok": True, "sent": False, "skipped": "disabled"}
    elif not daily_summary_enabled():
        result = {"ok": True, "sent": False, "skipped": "daily_summary_disabled"}
    else:
        result = _send_message(format_daily_summary(summary))
    result["date"] = summary["date"]
    result["sections"] = {
        "going_out": len(summary["going_out"]),
        "coming_back": len(summary["coming_back"]),
        "payment_due": summary["payment_due_count"],
    }
    return result


def send_test_message():
    return _send_message("✅ Sano Trailers Telegram notifications are connected.")
