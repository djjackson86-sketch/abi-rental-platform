from app import create_app

#: The plain-English section headings of the adapted (TrailerPro-style) Sano customer
#: notice, as they must read on the interim /privacy page that includes
#: templates/public/_customer_privacy_notice.html while nothing is published.
NOTICE_HEADINGS = (
    'Who collects your information?', 'What information is collected?',
    'Why is it collected?', 'Who has access to your information?',
    'How long is it kept?', 'How is it secured?', 'Where is it processed?',
    'Marketing', 'Your rights under POPIA', 'Information Regulator',
)


def test_short_notice_on_portal_and_privacy_preview(tmp_path, monkeypatch):
    from app.services import portal_intake
    monkeypatch.setattr(portal_intake, "registration_is_open", lambda: True)
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'privacy.db')})
    client = app.test_client()
    for url in ('/privacy',):
        response = client.get(url)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        for disclosure in (('customer privacy notice', 'Protection of Personal Information Act',
                            '229 Summit Road', 'Ireland', 'United States',
                            'alternative contact', 'Tax Administration Act',
                            'POPIAComplaints@inforegulator.org.za')
                           + NOTICE_HEADINGS):
            assert disclosure in text, (url, disclosure)
        assert 'complaints.IR@justice.gov.za' not in text
    portal = client.get('/portal').get_data(as_text=True)
    assert 'How long is it kept?' not in portal
    assert 'portal-privacy-template' not in portal
    assert portal.count('href="/privacy"') == 1
    assert portal.count('name="popia_consent"') == 1
    assert 'value="1" required' in portal
    assert 'may use my personal information' in portal
    assert 'name="marketing_opt_in"' in portal
    assert 'Sano Trailers customer privacy notice' in client.get('/privacy').get_data(as_text=True)
    assert 'being finalised' not in client.get('/privacy').get_data(as_text=True)
