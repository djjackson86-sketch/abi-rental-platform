#!/usr/bin/env python3
"""Small deployment smoke check for ABI Rental Platform.

Usage:
  python scripts/smoke_check.py http://127.0.0.1:5057
  ADMIN_EMAIL=... ADMIN_PASSWORD=... python scripts/smoke_check.py https://service.onrender.com
"""
from __future__ import annotations

import os
import sys
from http.cookiejar import CookieJar
from urllib import parse, request


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def fetch(opener, url: str, data: dict[str, str] | None = None):
    encoded = parse.urlencode(data).encode() if data is not None else None
    req = request.Request(url, data=encoded, method="POST" if data is not None else "GET")
    with opener.open(req, timeout=20) as response:
        body = response.read().decode("utf-8", "replace")
        return response.status, body, response.geturl()


def main() -> int:
    if len(sys.argv) != 2:
        fail("usage: python scripts/smoke_check.py <base-url>")
    base_url = sys.argv[1].rstrip("/")
    opener = request.build_opener(request.HTTPCookieProcessor(CookieJar()))

    status, body, _ = fetch(opener, f"{base_url}/health")
    if status != 200 or '"ok":true' not in body.replace(" ", ""):
        fail(f"/health failed: status={status} body={body[:200]!r}")

    status, body, _ = fetch(opener, f"{base_url}/store")
    if status != 200 or ("Online bookings" not in body and "Online booking is temporarily unavailable" not in body):
        fail("/store did not render the public store or unavailable state")

    admin_email = os.environ.get("ADMIN_EMAIL", "admin@abi.local")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    status, body, final_url = fetch(
        opener,
        f"{base_url}/login",
        {"email": admin_email, "password": admin_password},
    )
    if status != 200 or "/login" in final_url:
        fail("admin login failed; set ADMIN_EMAIL and ADMIN_PASSWORD for this environment")
    if "Continue setup" not in body and "Dashboard" not in body:
        fail("admin login did not reach an admin page")

    print(f"OK: smoke checks passed for {base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
