import html
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import current_app

from app.db import get_db
from app.services.orders import get_order, order_items


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def notifications_enabled():
    return _truthy(current_app.config.get("TELEGRAM_NOTIFICATIONS_ENABLED"))


def telegram_configured():
    return bool(current_app.config.get("TELEGRAM_BOT_TOKEN") and current_app.config.get("TELEGRAM_CHAT_ID"))


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


def _safe_send(builder):
    try:
        return _send_message(builder())
    except Exception as exc:
        current_app.logger.exception("Telegram notification failed: %s", exc)
        return {"ok": False, "sent": False, "error": str(exc)}


def format_customer_message(customer):
    return "\n".join([
        "👤 <b>New customer created</b>",
        f"Name: {_escape(customer['name'])}",
        f"Type: {_escape(customer['customer_type'])}",
        f"Email: {_escape(customer['email'] or 'Not supplied')}",
        f"Phone: {_escape(customer['phone'] or 'Not supplied')}",
        f"Marketing: {'Yes' if customer['marketing_opt_in'] else 'No'}",
    ])


def send_new_customer_notification(customer_id):
    def build():
        customer = get_db().execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not customer:
            return f"👤 <b>New customer created</b>\nCustomer ID: {_escape(customer_id)}"
        return format_customer_message(customer)
    return _safe_send(build)


def format_order_message(order, items):
    lines = [
        "📦 <b>New order / booking request</b>",
        f"Order: {_escape(order['order_number'])}",
        f"Customer: {_escape(order['customer_name'] or 'Not supplied')}",
        f"Email: {_escape(order['customer_email'] or 'Not supplied')}",
        f"Phone: {_escape(order['customer_phone'] or 'Not supplied')}",
        f"Pickup: {_escape(order['start_at'] or 'Not set')}",
        f"Return: {_escape(order['end_at'] or 'Not set')}",
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
    going_out = db.execute(
        """SELECT o.*, c.name AS customer_name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        WHERE DATE(o.start_at) = ? AND o.status != 'canceled' ORDER BY o.start_at, o.id""",
        (date_text,),
    ).fetchall()
    coming_back = db.execute(
        """SELECT o.*, c.name AS customer_name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        WHERE DATE(o.end_at) = ? AND o.status != 'canceled' ORDER BY o.end_at, o.id""",
        (date_text,),
    ).fetchall()
    due_row = db.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(due_total),0) AS total FROM orders WHERE payment_status = 'payment_due' AND status != 'canceled'"
    ).fetchone()
    return {"date": date_text, "going_out": going_out, "coming_back": coming_back, "payment_due_count": due_row["count"] or 0, "payment_due_total": due_row["total"] or 0}


def format_daily_summary(summary):
    lines = [f"📋 <b>ABI Rental daily summary for {_escape(summary['date'])}</b>", ""]
    lines.append(f"<b>Going out ({len(summary['going_out'])})</b>")
    if summary["going_out"]:
        for order in summary["going_out"][:20]:
            lines.append(f"• {_escape(order['order_number'])} — {_escape(order['customer_name'] or 'No customer')} at {_escape(order['start_at'] or '')}")
    else:
        lines.append("• None")
    lines.append("")
    lines.append(f"<b>Coming back ({len(summary['coming_back'])})</b>")
    if summary["coming_back"]:
        for order in summary["coming_back"][:20]:
            lines.append(f"• {_escape(order['order_number'])} — {_escape(order['customer_name'] or 'No customer')} at {_escape(order['end_at'] or '')}")
    else:
        lines.append("• None")
    lines.append("")
    lines.append(f"Payment due orders: {_escape(summary['payment_due_count'])} totaling {_money(summary['payment_due_total'])}")
    return "\n".join(lines)


def send_daily_summary(date_text=None):
    target_date = _parse_date(date_text)
    summary = daily_summary_counts(target_date)
    result = _send_message(format_daily_summary(summary))
    result["date"] = summary["date"]
    result["sections"] = {
        "going_out": len(summary["going_out"]),
        "coming_back": len(summary["coming_back"]),
        "payment_due": summary["payment_due_count"],
    }
    return result


def send_test_message():
    return _send_message("✅ ABI Rental Telegram notifications are connected.")
