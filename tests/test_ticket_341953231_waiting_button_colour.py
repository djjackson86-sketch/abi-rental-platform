"""ABI-341953231: scope the waiting action colour without changing submission."""
from bs4 import BeautifulSoup
from test_ticket_341953212_vehicle_approval import app, client, sign_in, manual_scan


def test_waiting_button_keeps_its_action_and_gets_scoped_colour(app, client):
    sign_in(client, app)
    response = manual_scan(client)
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, 'html.parser')
    buttons = soup.select('.scan-waiting-list')
    assert len(buttons) == 1
    button = buttons[0]
    assert button.get_text(strip=True) == 'Approve & place on waiting list'
    assert button['type'] == 'submit'
    assert button['value'] == 'waiting'
    assert button.has_attr('formnovalidate')
    assert button['onclick'] == "this.form.querySelector('[data-vehicle-action]').value='waiting'"
    assert button.find_parent('form')['action'] == '/scan-vehicle/save'
    assert 'scan-waiting-list' not in soup.select_one('#scan-approve-save')['class']
    assert 'scan-waiting-list' not in soup.select_one('#scan-disapprove-vehicle')['class']
    css = client.get('/static/css/app.css').get_data(as_text=True)
    assert '.btn.scan-waiting-list{background:#1d4ed8;color:#fff}' in css
    assert '.btn.scan-waiting-list:hover:not(:disabled)' in css
    assert '.btn.scan-waiting-list:focus-visible' in css
    assert '.btn.scan-waiting-list:disabled' in css
