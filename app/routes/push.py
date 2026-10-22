"""Staff Web Push endpoints: ``/push/config``, ``/push/subscribe``, ``/push/unsubscribe``,
``/push/test``.

All four are staff-only and speak JSON. They deliberately do **not** use
``login_required`` (which redirects to the login page): a background/API caller asking
for config should get a 401 it can act on, not an HTML login form.

Every POST validates the session's scoped CSRF token (``app/services/csrf.py``, scope
``push``) *before* anything is written, and the account is re-read from the database —
an ``active = 0`` account holding an old signed session cannot keep subscribing devices.
The push public key is the only key that ever leaves the server; the private VAPID key is
read by the service and never serialised into a response.
"""

from flask import Blueprint, jsonify, request, session

from app.db import get_db
from app.services import csrf
from app.services import push_notifications as push

bp = Blueprint("push", __name__, url_prefix="/push")


def current_active_user():
    """The signed-in account, re-read from the database, or ``None``.

    A session cookie alone is not enough: the account may have been deactivated or deleted
    since it was issued, so the row is the authority (the same rule staff screens follow).
    """
    user_id = session.get("user_id")
    try:
        user_id = int(user_id or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    return get_db().execute(
        "SELECT id, name, role, branch_id FROM users WHERE id = ? AND active = 1",
        (user_id,),
    ).fetchone()


def _refuse(message, status):
    return jsonify({"ok": False, "error": message}), status


def _require_user():
    user = current_active_user()
    if user is None:
        return None, _refuse("Please sign in again to manage notifications.", 401)
    return user, None


def _json_body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None
    return payload


@bp.route("/config", methods=["GET"])
def config():
    """What the browser needs to subscribe: the public key, whether push is on, CSRF token.

    ``enabled`` is false when the VAPID pair is not configured — the page then explains
    that notifications are not available instead of offering a button that cannot work.
    """
    user, refusal = _require_user()
    if refusal is not None:
        return refusal
    return jsonify(
        {
            "ok": True,
            "enabled": push.vapid_configured(),
            "library_available": push.library_available(),
            "vapid_public_key": push.vapid_credentials()[0],
            "csrf_token": csrf.issue_token(push.CSRF_SCOPE),
            "subscription_count": push.subscription_count(user["id"]),
            "ttl_seconds": push._int_setting("PUSH_TTL_SECONDS", 3600),
        }
    )


@bp.route("/subscribe", methods=["POST"])
def subscribe():
    user, refusal = _require_user()
    if refusal is not None:
        return refusal
    try:
        csrf.validate(push.CSRF_SCOPE)
    except Exception:
        # csrf.validate aborts with a 400 and a helpful description; convert it to JSON so
        # a fetch() caller gets a parseable body rather than the HTML error page.
        return _refuse(
            "Your session token is missing or has expired. Reload the page and try again.",
            400,
        )

    body = _json_body()
    if body is None:
        return _refuse("Send the subscription as a JSON object.", 400)
    # Accept the browser's PushSubscription.toJSON() either at the top level or nested.
    subscription = body.get("subscription") if isinstance(body.get("subscription"), dict) else body
    try:
        row = push.subscribe(
            user["id"], subscription, user_agent=request.headers.get("User-Agent", "")
        )
    except push.SubscriptionInvalid as exc:
        return _refuse(str(exc), 400)
    return jsonify(
        {
            "ok": True,
            "id": int(row["id"]),
            "subscription_count": push.subscription_count(user["id"]),
        }
    )


@bp.route("/unsubscribe", methods=["POST"])
def unsubscribe():
    user, refusal = _require_user()
    if refusal is not None:
        return refusal
    try:
        csrf.validate(push.CSRF_SCOPE)
    except Exception:
        return _refuse(
            "Your session token is missing or has expired. Reload the page and try again.",
            400,
        )

    body = _json_body() or {}
    endpoint = body.get("endpoint")
    if isinstance(body.get("subscription"), dict):
        endpoint = body["subscription"].get("endpoint", endpoint)
    if not str(endpoint or "").strip():
        return _refuse("Send the endpoint you want to stop notifying.", 400)
    # Only the caller's own binding can be removed (the service scopes the DELETE by user).
    removed = push.unsubscribe(user["id"], endpoint)
    return jsonify(
        {
            "ok": True,
            "removed": bool(removed),
            "subscription_count": push.subscription_count(user["id"]),
        }
    )


@bp.route("/test", methods=["POST"])
def test_notification():
    """Send one notification to the signed-in account's own devices.

    Nothing about a customer is included: this is a device check, so the payload is fixed
    text and the root URL.
    """
    user, refusal = _require_user()
    if refusal is not None:
        return refusal
    try:
        csrf.validate(push.CSRF_SCOPE)
    except Exception:
        return _refuse(
            "Your session token is missing or has expired. Reload the page and try again.",
            400,
        )
    if not push.vapid_configured():
        return _refuse("Push notifications are not configured on this server.", 400)
    result = push.send_test(user["id"])
    if not result.get("sent"):
        # Report the real reason rather than a cheerful "sent" the device never saw.
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "No notification could be delivered to this account's devices.",
                    "counts": result,
                }
            ),
            502,
        )
    return jsonify({"ok": True, "counts": result})
