"""ABI-341953194: one customer-prefilled action and light-green saved approval."""
from pathlib import Path
import re

from test_client_portal_frontend import app, client, branch, pending_client, scoped_csrf
from test_ticket_341953193_inline_portal_approval import prepare, approve


def test_saved_panel_has_only_customer_specific_order_action(client, app, pending_client):
    customer, registration = pending_client
    cid = customer['id']
    prepare(app, cid)
    client.get(f'/customers/{cid}')
    result = approve(client, registration, scoped_csrf(client), headers={'Accept': 'application/json'})
    assert result.status_code == 200
    for html in [result.get_json()['html']] + [
        client.get(path).get_data(as_text=True)
        for path in (f'/customers/{cid}', f'/customers/{cid}/edit')
    ]:
        match = re.search(r'<article[^>]*id="pending-registration".*?</article>', html, re.S)
        assert match
        panel = match.group()
        assert 'class="btn portal-approved" type="button" disabled>Approved</button>' in panel
        assert '>Create order</a>' not in panel
        assert panel.count('>Create order for this customer</a>') == 1
        assert f'href="/orders/new?customer_id={cid}"' in panel
    # The separate order-history entry is outside this ticket's scope.
    detail = client.get(f'/customers/{cid}').get_data(as_text=True)
    assert '>New order</a>' in detail
    assert '>Create order</a>' in detail


def test_light_green_rule_is_scoped_to_saved_approval_only():
    css = (Path(__file__).resolve().parents[1] / 'static/css/app.css').read_text(encoding='utf-8')
    rule = re.search(r'\.pending-registration-panel \.pending-approve-form \.btn\.portal-approved:disabled\{([^}]+)\}', css)
    assert rule
    for declaration in ('background:#dcfce7', 'color:#166534', 'border-color:#86efac', 'opacity:1'):
        assert declaration in rule.group(1)
    assert '.pending-approve-form .btn:disabled{opacity:.45;cursor:not-allowed}' in css


def test_pending_approval_keeps_its_existing_primary_button(client, pending_client):
    customer, _ = pending_client
    html = client.get(f"/customers/{customer['id']}").get_data(as_text=True)
    assert 'class="btn primary" type="submit" disabled' in html
    assert 'portal-approved' not in html
    assert 'Create order for this customer' in html
