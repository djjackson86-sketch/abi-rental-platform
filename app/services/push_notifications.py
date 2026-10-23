"""Web Push notifications for staff — subscription storage, safe validation, delivery.

Scope of this module (deliberately narrow):

* **One subscription per browser endpoint.** ``push_subscriptions.endpoint`` carries a
  UNIQUE index and the row is bound to the signed-in ``user_id``. A second account
  subscribing on the same browser *re-binds* that endpoint to the new account (the
  endpoint is a device secret, not a shared resource); it never creates a duplicate row,
  so one device can never be addressed twice for the same event.
* **Validation before storage.** Only the three real push services are accepted
  (Apple ``*.push.apple.com``, ``fcm.googleapis.com``, ``updates.push.services.mozilla.com``),
  only ``https`` on the default port, never a bare IP and never a URL carrying
  credentials or a fragment. The ``p256dh``/``auth`` keys must decode to the exact sizes
  of a P-256 subscription (65-byte uncompressed point, 16-byte auth secret); a malformed
  key is refused rather than stored and retried forever.
* **Delivery never raises into a caller that is saving business data.** Every send is
  bounded by a timeout and a TTL, a 404/410 means the browser is gone and the row is
  deleted, and other failures are counted on the row. A disabled row is never retried.
* **Recipients are per branch, never a broadcast.** ``notify_portal_submission`` reads the
  branch stored on the customer row and addresses only *active staff* whose primary branch
  is that branch or who were explicitly granted access to it. The owner (who sees every
  branch) and every other branch are deliberately excluded: a portal submission is a
  depot's work, not a company-wide alert. When the customer has no branch recorded, nobody
  is notified — an unknown branch is not "all branches". Each recipient's own
  ``notification_preferences`` row then decides whether *they* want customer alerts; a
  missing row means the type's default (ON), so existing subscribers keep their alerts.
* **Minimal PII.** The payload carries a title, a branch label and the admin URL
  ``/customers/<id>``. No name, phone, email or address ever leaves in a push payload.

``pywebpush`` is imported *optionally*: a host without it (the common local/CI case, and
this app's Windows laptop) must still import, migrate and serve the routes. Delivery then
reports ``skipped`` instead of failing.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
from urllib.parse import urlsplit

from flask import current_app, has_app_context

from app.db import get_db, now
from app.services import notification_preferences

#: The CSRF scope the push form/token pair uses (see app/services/csrf.py).
CSRF_SCOPE = "push"

#: Hosts a browser subscription may legitimately point at. Apple shards per browser
#: build, so it is matched as a suffix; the other two are the exact FCM / Mozilla hosts.
_EXACT_HOSTS = {
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
}
_APPLE_SUFFIX = ".push.apple.com"

#: Endpoints longer than this are not real subscription URLs (and would bloat the table).
MAX_ENDPOINT_LENGTH = 2048

#: Consecutive non-gone failures after which a subscription is switched off. It stays
#: as a row (so the same browser re-subscribing simply re-enables it) but is not retried.
MAX_FAILURES = 5

#: Statuses that mean "this browser will never accept a push again".
GONE_STATUSES = {404, 410}

#: Statuses that mean "our VAPID keys / TTL are wrong", not "the subscription is stale".
AUTH_STATUSES = {401, 403}


class SubscriptionInvalid(ValueError):
    """The posted subscription is not one we will store. The message is user-facing."""


class PushUnavailable(RuntimeError):
    """The optional ``pywebpush`` dependency is not installed on this host."""


class PushDeliveryError(RuntimeError):
    """One delivery attempt failed, carrying the provider's HTTP status when known."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- settings


def _setting(name, default=""):
    """Read a push setting from app config, falling back to the environment.

    The app factory owns ``app.config``; reading the environment as a fallback keeps this
    module usable in a background/script context (and means the Render VAPID variables
    already saved do not need a second wiring step to be seen).
    """
    value = None
    if has_app_context():
        value = current_app.config.get(name)
    if value in (None, ""):
        value = os.environ.get(name)
    if value in (None, ""):
        return default
    return str(value).strip()


def _int_setting(name, default):
    try:
        return int(_setting(name, default))
    except (TypeError, ValueError):
        return default


def vapid_credentials():
    """``(public_key, private_key, subject)`` exactly as configured, blanks included."""
    return (
        _setting("VAPID_PUBLIC_KEY"),
        _setting("VAPID_PRIVATE_KEY"),
        _setting("VAPID_SUBJECT", "mailto:admin@abi.local"),
    )


def vapid_configured():
    """True when both VAPID keys are present, i.e. a subscription could be delivered."""
    public_key, private_key, _ = vapid_credentials()
    return bool(public_key and private_key)


def library_available():
    try:
        import pywebpush  # noqa: F401

        return True
    except Exception:
        return False


# ------------------------------------------------------------------------- validation


def _b64url_length(value, label):
    """Decode a browser key and return its byte length, refusing anything unusable."""
    text = str(value or "").strip()
    if not text:
        raise SubscriptionInvalid(f"The browser did not supply its {label} key.")
    if len(text) > 256:
        raise SubscriptionInvalid(f"The browser's {label} key is not a valid key.")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise SubscriptionInvalid(f"The browser's {label} key is not a valid key.") from None
    return raw


def _host_allowed(host):
    host = str(host or "").lower().rstrip(".")
    if not host:
        return False
    if host in _EXACT_HOSTS:
        return True
    return host.endswith(_APPLE_SUFFIX) and len(host) > len(_APPLE_SUFFIX)


def validate_endpoint(endpoint):
    """Return the normalised endpoint, or raise :class:`SubscriptionInvalid`."""
    text = str(endpoint or "").strip()
    if not text:
        raise SubscriptionInvalid("The browser did not supply a push endpoint.")
    if len(text) > MAX_ENDPOINT_LENGTH:
        raise SubscriptionInvalid("That push endpoint is too long to be real.")
    try:
        parsed = urlsplit(text)
    except ValueError:
        raise SubscriptionInvalid("That push endpoint is not a valid URL.") from None
    if parsed.scheme.lower() != "https":
        raise SubscriptionInvalid("A push endpoint must use https.")
    if parsed.fragment:
        raise SubscriptionInvalid("A push endpoint must not carry a URL fragment.")
    if parsed.username or parsed.password:
        raise SubscriptionInvalid("A push endpoint must not carry credentials.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SubscriptionInvalid("That push endpoint has no host.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SubscriptionInvalid("A push endpoint must not be a bare IP address.")
    if not _host_allowed(host):
        raise SubscriptionInvalid("That push endpoint is not a recognised push service.")
    try:
        port = parsed.port
    except ValueError:
        raise SubscriptionInvalid("That push endpoint's port is not valid.") from None
    if port not in (None, 443):
        raise SubscriptionInvalid("A push endpoint must use the default https port.")
    return text


def validate_subscription(payload):
    """Normalise and validate a posted subscription.

    Returns ``{"endpoint", "p256dh", "auth"}``. Raises :class:`SubscriptionInvalid` with a
    message safe to show the signed-in user.
    """
    if not isinstance(payload, dict):
        raise SubscriptionInvalid("The subscription must be a JSON object.")
    # Accept both the flat form and the browser's own {endpoint, keys:{p256dh, auth}} shape.
    endpoint = payload.get("endpoint")
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        keys = payload
    endpoint = validate_endpoint(endpoint)

    p256dh = _b64url_length(keys.get("p256dh"), "p256dh")
    if len(p256dh) != 65 or p256dh[0] != 0x04:
        raise SubscriptionInvalid("The browser's p256dh key is not a valid P-256 public key.")
    auth = _b64url_length(keys.get("auth"), "auth")
    if len(auth) != 16:
        raise SubscriptionInvalid("The browser's auth secret is not a valid 16-byte key.")

    return {
        "endpoint": endpoint,
        "p256dh": str(keys.get("p256dh")).strip(),
        "auth": str(keys.get("auth")).strip(),
    }


# ---------------------------------------------------------------------- storage layer


def subscribe(user_id, payload, user_agent=""):
    """Store (or re-bind) one subscription for ``user_id``.

    The endpoint's UNIQUE index makes this a single row per browser: subscribing again as
    the same account refreshes the keys, subscribing as a different account moves the
    binding. Failures/disabled state are cleared, so a browser that re-subscribes after a
    410 comes back into service without a support call.
    """
    subscription = validate_subscription(payload)
    db = get_db()
    timestamp = now()
    existing = db.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint = ?", (subscription["endpoint"],)
    ).fetchone()
    if existing is None:
        db.execute(
            """INSERT INTO push_subscriptions
               (user_id, endpoint, p256dh, auth, user_agent, created_at, last_used_at,
                failure_count, disabled_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL)""",
            (
                int(user_id),
                subscription["endpoint"],
                subscription["p256dh"],
                subscription["auth"],
                str(user_agent or "")[:255],
                timestamp,
            ),
        )
    else:
        db.execute(
            """UPDATE push_subscriptions
               SET user_id = ?, p256dh = ?, auth = ?, user_agent = ?,
                   failure_count = 0, disabled_at = NULL
               WHERE id = ?""",
            (
                int(user_id),
                subscription["p256dh"],
                subscription["auth"],
                str(user_agent or "")[:255],
                existing["id"],
            ),
        )
    db.commit()
    return db.execute(
        "SELECT id, endpoint FROM push_subscriptions WHERE endpoint = ?",
        (subscription["endpoint"],),
    ).fetchone()


def unsubscribe(user_id, endpoint):
    """Remove the caller's own subscription for ``endpoint``. Another account's row stays.

    Returns True when a row was deleted. A crafted endpoint therefore cannot unsubscribe
    somebody else's browser.
    """
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False
    db = get_db()
    cur = db.execute(
        "DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?",
        (endpoint[:MAX_ENDPOINT_LENGTH], int(user_id)),
    )
    db.commit()
    return bool(cur.rowcount)


def subscription_count(user_id):
    row = get_db().execute(
        "SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ? AND disabled_at IS NULL",
        (int(user_id),),
    ).fetchone()
    return int(row[0] if row else 0)


def prune_stale_subscriptions(days=30):
    """Delete subscriptions disabled longer than ``days`` ago. Returns the row count.

    Called by the delivery path so a browser that vanished months ago does not sit in the
    table forever. Uses the database clock string format, so it compares lexically like
    every other timestamp in this app.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    db = get_db()
    cur = db.execute(
        "DELETE FROM push_subscriptions WHERE disabled_at IS NOT NULL AND disabled_at < ?",
        (cutoff,),
    )
    db.commit()
    return int(cur.rowcount or 0)


# -------------------------------------------------------------------------- delivery


def _dispatch(subscription, payload_json):
    """One network send. Kept as a module function so tests can stand in for pywebpush."""
    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:  # ImportError and any broken install
        raise PushUnavailable("pywebpush is not installed on this host") from exc

    public_key, private_key, subject = vapid_credentials()
    if not (public_key and private_key):
        raise PushUnavailable("VAPID keys are not configured")
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=payload_json,
            vapid_private_key=private_key,
            vapid_claims={"sub": subject},
            ttl=_int_setting("PUSH_TTL_SECONDS", 3600),
            timeout=_int_setting("PUSH_TIMEOUT_SECONDS", 10),
        )
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        raise PushDeliveryError(str(exc), status=getattr(response, "status_code", None)) from exc
    except (ValueError, TypeError) as exc:
        # pywebpush raises ValueError/TypeError when the *server's* VAPID key (or the
        # subscription key material) cannot be deserialised. That is a configuration fault,
        # not this device's: report it as unavailable so one bad key cannot march every
        # subscription up to the failure limit and switch the whole book off.
        raise PushUnavailable(f"push key/configuration error: {exc}") from exc


def _empty_counts(recipients=0):
    return {
        "recipients": int(recipients),
        "sent": 0,
        "failed": 0,
        "removed": 0,
        "disabled": 0,
        "skipped": 0,
    }


def send_to_users(user_ids, payload):
    """Deliver ``payload`` to every live subscription of ``user_ids``.

    Returns counts (``recipients``, ``sent``, ``failed``, ``removed``, ``disabled``,
    ``skipped``). Never raises for a delivery problem: the caller may be mid-save.
    """
    counts = _empty_counts(0)
    ids = sorted({int(uid) for uid in (user_ids or []) if uid})
    if not ids:
        return counts

    db = get_db()
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT id, endpoint, p256dh, auth FROM push_subscriptions
            WHERE user_id IN ({marks}) AND disabled_at IS NULL
            ORDER BY id""",
        ids,
    ).fetchall()
    counts["recipients"] = len(ids)
    if not rows:
        counts["recipients"] = 0
        return counts

    if not vapid_configured() or not library_available():
        # Nothing can go out; report it as skipped rather than pretending success.
        counts["skipped"] = len(rows)
        return counts

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    timestamp = now()
    for row in rows:
        subscription = {
            "endpoint": row["endpoint"],
            "p256dh": row["p256dh"],
            "auth": row["auth"],
        }
        try:
            validate_subscription({"endpoint": subscription["endpoint"], "keys": subscription})
            _dispatch(subscription, payload_json)
        except PushUnavailable:
            counts["skipped"] += 1
            continue
        except PushDeliveryError as exc:
            if exc.status in GONE_STATUSES:
                # The browser is gone (uninstalled, permission revoked, profile wiped).
                # Remove it now: retrying can only fail again.
                db.execute("DELETE FROM push_subscriptions WHERE id = ?", (row["id"],))
                counts["removed"] += 1
                continue
            counts["failed"] += 1
            if exc.status in AUTH_STATUSES:
                # Our keys are wrong, not the subscription: do not count it against the row.
                continue
            db.execute(
                "UPDATE push_subscriptions SET failure_count = COALESCE(failure_count, 0) + 1 "
                "WHERE id = ?",
                (row["id"],),
            )
            failure_row = db.execute(
                "SELECT failure_count FROM push_subscriptions WHERE id = ?", (row["id"],)
            ).fetchone()
            if failure_row is not None and int(failure_row[0] or 0) >= MAX_FAILURES:
                db.execute(
                    "UPDATE push_subscriptions SET disabled_at = ? WHERE id = ?",
                    (timestamp, row["id"]),
                )
                counts["disabled"] += 1
            continue
        except Exception:
            # A library/transport surprise is one failed device, never an exception for
            # the caller that is saving a customer.
            counts["failed"] += 1
            continue
        db.execute(
            "UPDATE push_subscriptions SET last_used_at = ?, failure_count = 0 WHERE id = ?",
            (timestamp, row["id"]),
        )
        counts["sent"] += 1

    db.commit()
    try:
        prune_stale_subscriptions()
    except Exception:
        # Housekeeping only.
        pass
    return counts


# ----------------------------------------------------------------- portal recipients

#: The exact SQL that decides who hears about a portal submission.
#: * ``role = 'staff'`` — the owner is excluded by the role test itself (an owner's
#:   ``can_view_all_branches`` must never turn one depot's submission into a broadcast).
#: * the customer's **persisted** branch, either as the staff member's primary branch or
#:   through an explicit ``user_branch_access`` grant. A NULL/missing branch matches
#:   nothing, so an unassigned customer is silent rather than broadcast.
_RECIPIENT_SQL = """
    SELECT DISTINCT u.id
    FROM users u
    WHERE u.active = 1
      AND u.role = 'staff'
      AND (
            u.branch_id = :branch_id
         OR EXISTS (
                SELECT 1 FROM user_branch_access a
                WHERE a.user_id = u.id AND a.branch_id = :branch_id
            )
      )
    ORDER BY u.id
"""


def portal_recipients(customer_id):
    """``(user_ids, branch_id, branch_name)`` for a persisted customer row."""
    row = get_db().execute(
        """SELECT c.id, c.branch_id, b.name AS branch_name
           FROM customers c LEFT JOIN branches b ON b.id = c.branch_id
           WHERE c.id = ?""",
        (int(customer_id),),
    ).fetchone()
    if row is None:
        return [], None, ""
    branch_id = row["branch_id"]
    try:
        branch_id = int(branch_id)
    except (TypeError, ValueError):
        return [], None, ""
    if branch_id <= 0:
        return [], None, ""
    recipients = [
        int(user["id"] if hasattr(user, "keys") else user[0])
        for user in get_db().execute(_RECIPIENT_SQL, {"branch_id": branch_id}).fetchall()
    ]
    return recipients, branch_id, str(row["branch_name"] or "")


def _portal_url(customer_id):
    """The admin URL in the payload. Always a ``/customers/<id>`` path, never PII."""
    path = f"/customers/{int(customer_id)}"
    base = _setting("PUBLIC_BASE_URL").rstrip("/")
    return f"{base}{path}" if base else path


def notify_portal_submission(customer_id):
    """Tell the depot's staff that a client submitted the portal form.

    Called **once**, after the customer write and its receipt commit, at the same point as
    the Telegram alert. Returns the delivery counts; on any failure it returns
    ``{"error": ...}`` and logs, so the customer save is never affected.
    """
    try:
        recipients, branch_id, branch_name = portal_recipients(customer_id)
        counts = _empty_counts(len(recipients))
        # The resolved depot is reported even when it has no subscribers: the caller's logs
        # should show *where* the submission landed, and 0 recipients must not look like a
        # branch that does not exist.
        counts["branch_id"] = int(branch_id) if branch_id else 0
        if not recipients:
            return counts
        # Each person's own settings decide whether they hear about customer alerts. A
        # missing preference row means the type's default (ON), so existing subscribers
        # keep the alerts they already receive.
        wanted = notification_preferences.enabled_user_ids(
            recipients, notification_preferences.CUSTOMER_NOTIFICATIONS
        )
        counts["recipients"] = len(wanted)
        if not wanted:
            return counts
        recipients = wanted
        payload = {
            "title": "New customer portal submission",
            "body": f"A client submitted their details at {branch_name or 'your branch'}.",
            "url": f"/customers/{int(customer_id)}",
            "customer_id": int(customer_id),
            "tag": f"portal-submission-{int(customer_id)}",
        }
        result = send_to_users(recipients, payload)
        result["recipients"] = len(recipients)
        result["branch_id"] = int(branch_id)
        return result
    except Exception as exc:
        try:
            current_app.logger.exception(
                "Portal push notification failed for customer %s", customer_id
            )
        except Exception:
            pass
        return {"error": str(exc), **_empty_counts()}
