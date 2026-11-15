"""Ticket ABI-341953309: the portal phone/WhatsApp checkbox says what it means.

The client asked for the terse "Same for WhatsApp" label to become the full
sentence, so a customer reading the form knows the box copies the cellphone
number into WhatsApp. The behaviour (name, checked state, hiding the separate
WhatsApp field) is unchanged and covered by the WhatsApp ticket tests.
"""
from bs4 import BeautifulSoup

from test_client_portal_frontend import app, branch, client, slug_of  # noqa: F401


def test_whatsapp_checkbox_uses_the_new_full_wording(client, app, branch):
    page = client.get(f'/portal/{slug_of(app, branch)}').get_data(as_text=True)
    assert 'This is the same number for WhatsApp' in page
    assert 'Same for WhatsApp' not in page

    soup = BeautifulSoup(page, 'html.parser')
    checkbox = soup.select_one('input[name="whatsapp_same_as_phone"]')
    assert checkbox is not None
    label = checkbox.find_parent('label')
    assert label is not None
    assert 'This is the same number for WhatsApp' in label.get_text(' ', strip=True)
    # The toggle still drives the separate WhatsApp number field.
    assert label.input.get('aria-controls') == 'portal-whatsapp'
