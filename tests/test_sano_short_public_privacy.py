from app import create_app


def test_short_notice_on_portal_and_privacy_preview(tmp_path):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'privacy.db')})
    client = app.test_client()
    for url in ('/privacy',):
        response = client.get(url)
        assert response.status_code == 200
        text = response.get_data(as_text=True)
        for disclosure in ('Customer privacy notice', 'Protection of Personal Information Act',
                           '229 Summit Road', 'What we collect and why', 'Your choices',
                           'Who receives it', 'Overseas processing', 'Ireland', 'United States',
                           'Keeping your information', 'Marketing', 'Your rights',
                           'alternative contact', 'Tax Administration Act',
                           'POPIAComplaints@inforegulator.org.za'):
            assert disclosure in text, (url, disclosure)
        assert 'complaints.IR@justice.gov.za' not in text
    portal = client.get('/portal').get_data(as_text=True)
    assert 'What we collect and why' not in portal
    assert 'portal-privacy-template' not in portal
    assert portal.count('href="/privacy"') == 1
    assert portal.count('name="popia_consent"') == 1
    assert 'value="1" required' in portal
    assert 'This does not subscribe me to marketing' in portal
    assert 'name="marketing_opt_in"' in portal
    assert 'Our privacy notice is being finalised' in client.get('/privacy').get_data(as_text=True)
