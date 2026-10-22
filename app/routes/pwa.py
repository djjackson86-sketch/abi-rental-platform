"""The installable-app surface: the root service worker and the web manifest.

A service worker registered from ``/static/sw.js`` can only control ``/static/``
unless the response carries ``Service-Worker-Allowed``. Both files are therefore
served from the site root so the worker's scope is the whole app:

* ``/sw.js`` — the push/notification worker (``static/sw.js``). Deliberately
  served **uncached**: a stale worker that never updates is worse than an extra
  conditional request, and the file is tiny.
* ``/manifest.webmanifest`` — "Sano Trailers" installed as a standalone app,
  using the Sano icons that already ship in ``static/img``.

Neither route reads or writes the database, so it is safe to serve them to a
signed-out browser (the install prompt has to work before anyone signs in). The
worker itself never caches a response: authenticated screens are not offline
content.
"""

from flask import Blueprint, current_app, jsonify, send_from_directory

bp = Blueprint("pwa", __name__)


def _no_cache(response):
    """Stop intermediaries and the browser reusing the worker/manifest bytes."""
    response.headers["Cache-Control"] = "no-cache, no-store, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.route("/sw.js")
def service_worker():
    """Serve the push service worker for scope ``/``."""
    response = send_from_directory(
        current_app.static_folder or current_app.root_path,
        "sw.js",
        mimetype="text/javascript",
        max_age=0,
        conditional=True,
    )
    # The whole app is in scope, so a notification click can land on /customers/7.
    response.headers["Service-Worker-Allowed"] = "/"
    return _no_cache(response)


@bp.route("/manifest.webmanifest")
def manifest():
    """The standalone install manifest, pointing at the existing Sano icons."""
    payload = {
        "id": "/",
        "name": "Sano Trailers",
        "short_name": "Sano",
        "description": "Sano Trailers rentals, orders and customer portal.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#ffffff",
        "theme_color": "#0b4ccc",
        "icons": [
            {
                "src": "/static/img/favicon-180.png",
                "sizes": "180x180",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/static/img/favicon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
        ],
        "prefer_related_applications": False,
    }
    response = jsonify(payload)
    response.mimetype = "application/manifest+json"
    return _no_cache(response)
