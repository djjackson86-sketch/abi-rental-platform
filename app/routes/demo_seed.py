"""TEMPORARY: token-guarded demo seed for the isolated side service.

The side service's database is a SQLite file on its own Render disk
(``/var/data/abi_rental.db``), which cannot be written from a developer machine
and cannot be mounted by a one-off Render job. So the running service seeds
itself: this route runs the same idempotent ``scripts.seed_side_demo.seed`` the
CLI uses, writing to whatever database the process is configured with.

Guard: the caller must present ``sha256("demo-seed:" + SECRET_KEY)``, so no new
secret is introduced and nothing has to be added to the service's environment.
POST only, and the route is removed (with a redeploy) once the demo data is in.
"""

import hashlib
import hmac

from flask import Blueprint, current_app, jsonify, request

bp = Blueprint("demo_seed", __name__)


def _expected_token():
    secret = current_app.config.get("SECRET_KEY", "") or ""
    return hashlib.sha256(("demo-seed:" + secret).encode()).hexdigest()


@bp.post("/internal/demo-seed")
def demo_seed():
    supplied = request.headers.get("X-Demo-Seed", "") or request.args.get("token", "")
    if not supplied or not hmac.compare_digest(supplied, _expected_token()):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    from scripts.seed_side_demo import seed

    result = seed(app=current_app)
    return jsonify({"ok": True, "result": result})
