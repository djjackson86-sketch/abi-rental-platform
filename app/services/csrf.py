"""Scoped CSRF tokens for the new owner-only POST forms (facility/portal + POPIA wizard).

Deliberately **scoped**, not an app-wide retrofit. Turning on Flask-WTF style CSRF for
every form would break the many existing staff forms that post without a token, so this
module protects only the surfaces added by the customer-portal / POPIA programme:

* the portal settings save (``settings.portal_save``), and
* the POPIA setup wizard saves and publish (``settings.popia_wizard_save_step*`` /
  ``settings.popia_wizard_publish``).

Each scope gets one random token stored in the signed session cookie. The matching
hidden field is rendered into the form and checked server-side with a constant-time
compare; a missing, empty or mismatched token is refused with a 400 and **nothing is
written**. That is a double-submit-style defence: a cross-site page can make the browser
POST, but it cannot read the token out of the victim's signed session to put it in the
body.

No IP address, no user agent — the only thing stored is the random token, in the session.
"""

import secrets

from flask import abort, request, session

#: Where the per-scope tokens live in the session cookie.
_SESSION_KEY = "_csrf_tokens"

#: The hidden field the forms carry and the header a scripted client may use instead.
FORM_FIELD = "csrf_token"
HEADER_FIELD = "X-CSRF-Token"


def issue_token(scope, rotate=False):
    """Return the session's token for ``scope``, creating it on first use.

    ``rotate=True`` mints a fresh value (used after a successful write so a leaked token
    cannot be replayed indefinitely). The token is bound to the signed session cookie.
    """
    tokens = session.get(_SESSION_KEY)
    if not isinstance(tokens, dict):
        tokens = {}
    token = None if rotate else tokens.get(scope)
    if not token:
        token = secrets.token_urlsafe(32)
        tokens[scope] = token
        session[_SESSION_KEY] = tokens
    return token


def _supplied():
    value = request.form.get(FORM_FIELD)
    if value is None:
        value = request.headers.get(HEADER_FIELD, "")
    return str(value or "").strip()


def validate(scope):
    """Refuse the request with 400 unless it carries this session's token for ``scope``.

    Called at the very top of the protected views, so a refused POST writes nothing.
    """
    tokens = session.get(_SESSION_KEY)
    expected = tokens.get(scope) if isinstance(tokens, dict) else None
    supplied = _supplied()
    if not expected or not supplied or not secrets.compare_digest(str(expected), supplied):
        abort(
            400,
            description=(
                "Your session token is missing or has expired. Reload the page and try again."
            ),
        )
