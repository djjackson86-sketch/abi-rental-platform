"""Frontend/PWA for staff push notifications, and safe sign-in continuation.

Two things are pinned here that the push backend tests cannot see:

* the installable-app surface — ``/sw.js`` served from the site root with
  ``Service-Worker-Allowed: /`` and no caching, and ``/manifest.webmanifest``
  describing Sano Trailers as a standalone app using the icons that already ship;
* the browser side of the opt-in — deliberate tap only (no prompt and no
  subscription on page load), iOS Home-Screen guidance before capability checks,
  one-endpoint unsubscribe on logout, a control in the **main column** (the
  sidebar links are hidden at <=860px), and the worker showing a minimal payload
  and opening only local ``/customers/<id>`` screens.

``next`` continuation is checked too, because it is the other way a crafted link
could be made to leave the site.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from app import create_app
from app.db import get_db
from app.services.access import MODULE_KEYS

ROOT = Path(__file__).resolve().parents[1]
SW = ROOT / "static" / "sw.js"
PUSH_JS = ROOT / "static" / "js" / "push.js"
LAYOUT = ROOT / "templates" / "admin" / "layout.html"
BASE = ROOT / "templates" / "base.html"
HARNESS = ROOT / "tests" / "js" / "push_service_worker.mjs"
NODE = shutil.which("node")


@pytest.fixture()
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.db"),
            "SECRET_KEY": "test",
            "ADMIN_EMAIL": "admin@abi.local",
            "ADMIN_PASSWORD": "admin123",
        }
    )
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


def owner_id(app):
    with app.app_context():
        row = get_db().execute("SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        assert row is not None
        return row["id"]


def sign_in(client, app, name=None, password="admin123", follow=True, data=None):
    with client.application.app_context():
        if name is None:
            row = get_db().execute(
                "SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1"
            ).fetchone()
        else:
            row = get_db().execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        user_id = row["id"] if row else None
    payload = {"user_id": str(user_id), "password": password}
    payload.update(data or {})
    return client.post("/login", data=payload, follow_redirects=follow)


def add_two_depot_staff(client, app, name="Two Depot", password="staff123"):
    sign_in(client, app)
    client.post("/settings/users/permissions", data={"module": list(MODULE_KEYS)}, follow_redirects=True)
    client.post(
        "/settings/users/add",
        data={"name": name, "password": password, "branch_ids": ["1", "2"]},
        follow_redirects=True,
    )
    client.post("/logout")
    return name, password


def hidden_next(html):
    match = re.search(r'<input type="hidden" name="next" value="([^"]*)"', html)
    return match.group(1) if match else None


def main_column(html):
    start = html.find("<main")
    end = html.find("</main>")
    assert start != -1 and end != -1
    return html[start:end]


def sidebar_column(html):
    start = html.find('<aside class="sidebar">')
    end = html.find("</aside>")
    assert start != -1 and end != -1
    return html[start:end]


# --- the installable-app surface --------------------------------------------


def test_the_worker_is_served_from_the_site_root_uncached(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    # Scope / is the whole point: a worker under /static/ could not open /customers/7.
    assert response.headers["Service-Worker-Allowed"] == "/"
    cache_control = response.headers["Cache-Control"].lower()
    assert "no-cache" in cache_control and "no-store" in cache_control
    assert "max-age=0" in cache_control
    body = response.get_data(as_text=True)
    assert "addEventListener('push'" in body
    assert "showNotification" in body
    assert "'/customers/'" in body
    assert "notificationclick" in body
    assert "openWindow" in body


def test_the_worker_keeps_no_cache_of_authenticated_pages(client):
    body = client.get("/sw.js").get_data(as_text=True)
    # No fetch handler and no CacheStorage: staff screens are never offline content.
    assert "caches" not in body
    assert "addEventListener('fetch'" not in body
    assert "addEventListener(\"fetch\"" not in body


def test_the_manifest_installs_sano_standalone_and_uses_icons_that_exist(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert "manifest" in response.mimetype
    manifest = json.loads(response.get_data(as_text=True))
    assert manifest["name"] == "Sano Trailers"
    assert manifest["short_name"] == "Sano"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["icons"], "an installable app needs at least one icon"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"180x180", "512x512"} <= sizes
    for icon in manifest["icons"]:
        src = icon["src"]
        assert src.startswith("/static/img/"), src
        assert (ROOT / src.lstrip("/")).is_file(), src


def test_pages_advertise_the_manifest_and_home_screen_metadata(client):
    html = client.get("/login").get_data(as_text=True)
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert '<meta name="theme-color"' in html
    assert '<meta name="apple-mobile-web-app-capable" content="yes">' in html
    assert '<meta name="apple-mobile-web-app-title" content="Sano">' in html


# --- worker behaviour, exercised in Node ------------------------------------


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_worker_shows_the_payload_and_only_opens_local_customer_screens():
    node = NODE
    assert node, "node is required for this harness"
    completed = subprocess.run(
        [node, str(HARNESS), str(SW)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)

    assert set(result["listeners"]) == {"install", "activate", "notificationclick", "push"}

    # The payload decides the text; a customer id becomes a local customer path.
    assert result["customer"]["title"] == "New customer"
    assert result["customer"]["url"] == "/customers/12"
    assert result["customer"]["icon"].startswith("/static/img/")
    assert result["customer"]["tag"] == "customer-12"

    # A minimal/empty payload still shows something and falls back to the app root.
    assert result["empty"]["title"] == "Sano Trailers"
    assert result["empty"]["url"] == "/"
    assert result["hostileId"] == "/"
    assert result["oddTypes"]["title"] == "Sano Trailers"
    assert result["oddTypes"]["url"] == "/customers/7"

    # Clicking focuses the app that is already open, then navigates to the customer.
    assert result["clickExisting"] == {"focused": True, "navigated": "/customers/4", "opened": []}
    # A window on another origin is neither focused nor navigated; a local one opens.
    assert result["clickForeign"] == {"focused": False, "navigated": None, "opened": ["/customers/5"]}
    # Nothing external can be opened, whatever the payload or the click data says.
    assert result["clickUnsafe"]["opened"] == ["/"]
    assert result["clickGarbage"]["opened"] == ["/"]
    assert result["clickGarbage"]["backslash"] == ["/"]
    assert result["clickNewWindow"]["opened"] == ["/customers/9"]


# --- the browser side of the opt-in -----------------------------------------


def test_the_opt_in_needs_a_deliberate_tap():
    source = PUSH_JS.read_text(encoding="utf-8")
    # Exactly one permission prompt, inside the button handler - never on load.
    assert source.count("Notification.requestPermission") == 1
    handler = source[source.find("function enableNotifications") : source.find("function sendTestNotification")]
    assert "Notification.requestPermission" in handler
    before = source[: source.find("function enableNotifications")]
    assert "Notification.requestPermission" not in before
    # And the button is the only thing wired to it.
    assert "enableBtn.addEventListener('click', enableNotifications)" in source


def test_nothing_is_subscribed_on_page_load_unless_permission_is_already_granted():
    source = PUSH_JS.read_text(encoding="utf-8")
    assert source.count("pushManager.subscribe(") == 1
    init = source[source.find("function init()") :]
    # Load-time path: config GET, then the shared re-register only when granted.
    assert "loadConfig()" in init
    assert "Notification.permission !== 'granted'" in init
    assert "registerSubscription()" in init
    # The shared helper is the one place a subscription is created.
    registrar = source[source.find("function registerSubscription") : source.find("function enableNotifications")]
    assert "pushManager.subscribe(" in registrar
    assert "SUBSCRIBE_URL" in registrar
    assert "/push/subscribe" in source
    assert "X-CSRF-Token" in source, "the scoped CSRF token goes in the header"
    assert "csrf_token" in source


def test_ios_home_screen_guidance_comes_before_the_capability_check():
    source = PUSH_JS.read_text(encoding="utf-8")
    init = source[source.find("function init()") :]
    ios_at = init.find("isIOS() && !isStandalone()")
    capability_at = init.find("!supported()")
    assert ios_at != -1 and capability_at != -1
    assert ios_at < capability_at, "Safari in a tab has no PushManager; guidance must win"
    assert "Add to Home Screen" in init


def test_logout_unsubscribes_this_browser_only(client, app):
    source = PUSH_JS.read_text(encoding="utf-8")
    assert "/push/unsubscribe" in source
    # The endpoint is sent before the session ends, then the provider is dropped.
    handler = source[source.find("function leavePush") : source.find("function onLogoutSubmit")]
    assert "notifyServerUnsubscribe" in handler
    assert handler.find("notifyServerUnsubscribe") < handler.find("subscription.unsubscribe()")
    assert "form[action$=\"/logout\"]" in source
    # The logout form carries the endpoint so a no-JS/slow path still scopes it.
    layout = LAYOUT.read_text(encoding="utf-8")
    assert 'name="push_endpoint"' in layout
    form = re.search(r"<form[^>]*auth\.logout[^>]*>.*?</form>", layout, re.S)
    assert form and 'name="push_endpoint"' in form.group(0)
    # And POST /logout still clears the session whatever the field holds.
    sign_in(client, app)
    response = client.post("/logout", data={"push_endpoint": "https://push.example/abc"})
    assert response.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_the_control_is_in_the_main_column_so_a_phone_can_reach_it(client, app):
    sign_in(client, app)
    html = client.get("/dashboard").get_data(as_text=True)

    main = main_column(html)
    assert 'data-push-controls' in main
    assert 'data-push-enable' in main and "Enable notifications" in main
    assert 'data-push-test' in main and "Test notification" in main
    # Never only in the sidebar: its links are hidden at <=860px.
    assert "data-push-controls" not in sidebar_column(html)
    # The behaviour ships with the page.
    assert "js/push.js" in html

    style = html[html.find("<style>") : html.find("</style>")]
    mobile = re.search(r"@media\(max-width:860px\)\{(.*?)\}\s*@media", style, re.S)
    assert mobile, "the compact bar needs a phone rule"
    assert ".push-controls" in mobile.group(1)
    assert "display:none" not in mobile.group(1)


def test_the_layout_does_not_prompt_or_subscribe_by_itself():
    layout = LAYOUT.read_text(encoding="utf-8")
    assert "requestPermission" not in layout
    assert "pushManager" not in layout
    assert 'src="{{ url_for(\'static\', filename=\'js/push.js\') }}" defer' in layout


# --- safe sign-in continuation ----------------------------------------------


def test_the_sign_in_wall_remembers_where_the_request_was_going(client):
    response = client.get("/orders")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("/login?next=")
    assert unquote(location).endswith("/orders")
    # A POST body cannot be replayed, so it is not remembered.
    response = client.post("/customers/new", data={})
    assert response.status_code == 302
    assert response.headers["Location"] == "/login"


def test_a_local_target_survives_the_sign_in(client, app):
    page = client.get("/login?next=/orders").get_data(as_text=True)
    assert hidden_next(page) == "/orders"

    response = sign_in(client, app, follow=False, data={"next": "/orders"})
    assert response.status_code == 302
    assert response.headers["Location"] == "/orders"
    assert client.get("/orders").status_code == 200


def test_the_depot_choice_keeps_the_target(client, app):
    name, password = add_two_depot_staff(client, app)

    response = sign_in(client, app, name=name, password=password, follow=False, data={"next": "/customers"})
    assert response.status_code == 302
    location = response.headers["Location"]
    assert urlsplit(location).path == "/select-branch"
    assert unquote(location).endswith("next=/customers")

    page = client.get(location).get_data(as_text=True)
    assert hidden_next(page) == "/customers"

    response = client.post(
        "/select-branch", data={"branch_id": "2", "next": "/customers"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["Location"] == "/customers"
    with client.session_transaction() as sess:
        assert sess["active_branch_id"] == 2


@pytest.mark.parametrize(
    "hostile",
    [
        "//evil.example/customers",
        "/\\evil.example/customers",
        "\\\\evil.example\\customers",
        "https://evil.example/customers",
        "http:/evil.example",
        "javascript:alert(1)",
        "\\\\evil",
        "customers",
        "/login",
        "/logout",
        "/login?next=/orders",
        "/\t/evil.example",
    ],
)
def test_a_target_can_never_leave_the_site(client, app, hostile):
    page = client.get("/login", query_string={"next": hostile}).get_data(as_text=True)
    assert hidden_next(page) is None or hidden_next(page) == ""

    response = sign_in(client, app, follow=False, data={"next": hostile})
    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard"


def test_an_ordinary_sign_in_lands_exactly_where_it_did_before(client, app):
    response = sign_in(client, app, follow=False)
    assert response.status_code == 302
    assert response.headers["Location"] == "/dashboard"
    assert hidden_next(client.get("/login").get_data(as_text=True)) is None


def test_a_branch_choice_without_a_target_still_lands_on_the_dashboard(client, app):
    name, password = add_two_depot_staff(client, app)
    response = sign_in(client, app, name=name, password=password, follow=False)
    assert urlsplit(response.headers["Location"]).path == "/select-branch"
    response = client.post("/select-branch", data={"branch_id": "1"}, follow_redirects=False)
    assert response.headers["Location"] == "/dashboard"
