"""TEMPORARY internal endpoint used to load demo data into an isolated instance.

Guarded by the ``DEMO_SEED_TOKEN`` environment secret. It exists only long enough to
seed the separate side instance for testing and is removed again straight afterwards.
"""

import os

from flask import Blueprint, jsonify, request

bp = Blueprint("internal_seed", __name__, url_prefix="/api/internal/seed")


def _token():
    return (os.environ.get("DEMO_SEED_TOKEN") or "").strip()


@bp.post("/demo")
def seed_demo():
    expected = _token()
    if not expected:
        return jsonify({"ok": False, "error": "seeding is not enabled"}), 404
    supplied = (request.headers.get("X-Seed-Token") or request.form.get("token") or "").strip()
    if supplied != expected:
        return jsonify({"ok": False, "error": "not found"}), 404
    from scripts.seed_side_demo import seed

    return jsonify({"ok": True, "result": seed()})
