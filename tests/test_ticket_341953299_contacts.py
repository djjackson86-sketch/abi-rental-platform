"""Staff panel, shared partial-save and historical contact regression checks."""
import json
import threading
from pathlib import Path
import os
import pytest
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app.db import get_db
from app.services.customers import create_customer, get_customer, update_customer, raw_custom_fields_for
from test_client_portal_frontend import app, client


def owner(client):
    with client.session_transaction() as s:
        s.update(user_id=1, user_role="owner", user_name="Test owner")


def company(**extra):
    return dict(name="Synthetic Business", customer_type="company", phone="0821234567",
                contact_details_panel="1", contact_person_name="Test Person",
                director_name="Director Tester", director_contact="0831234567", **extra)


@pytest.mark.parametrize("director", ["0", "1"])
def test_staff_create_edit_persistence_and_redisplay(app, client, director):
    owner(client)
    data=company(whatsapp_number="0841234567", telephone_number="0111234567", contact_person_is_director=director)
    if director == "1":
        data.pop("director_name"); data.pop("director_contact")
    response=client.post("/customers/new", data=data)
    assert response.status_code == 302
    url=response.headers["Location"]
    html=client.get(url+"/edit").get_data(as_text=True)
    soup=BeautifulSoup(html,"html.parser")
    for key in ["whatsapp_number","telephone_number","contact_person_name"]:
        assert soup.select_one(f'[name="{key}"]')["value"] == data[key]
    assert soup.select_one('[name="contact_person_is_director"]').has_attr("checked") == (director=="1")
    data["whatsapp_number"]=""
    assert client.post(url+"/edit",data=data).status_code==302
    with app.app_context():
        row=get_customer(int(url.rsplit("/",1)[1])); raw=raw_custom_fields_for(row)
        assert "whatsapp_number" not in raw
        assert raw["contact_person_is_director"]==director
    bad=dict(data, director_name="", contact_person_is_director="0", contact_person_name="Kept on error")
    html=client.post(url+"/edit",data=bad).get_data(as_text=True)
    assert "Directors Name &amp; Surname is required" in html
    assert 'value="Kept on error"' in html
    assert client.get(url).status_code == 200


def test_shared_inline_save_preserves_new_fields_and_legacy_alternative(app):
    with app.app_context():
        cid=create_customer(dict(name="Legacy Business",customer_type="company", alternative_contact_name="Historical Relative", alternative_contact_number="0800000000"),notify=False)
        update_customer(cid,company(contact_person_is_director="0",whatsapp_number="0841234567"))
        raw=raw_custom_fields_for(get_customer(cid))
        assert raw["alternative_contact_name"]=="Historical Relative"
        assert raw["director_name"]=="Director Tester"
        # Existing inline order payload has no new panel; no new required rules.
        update_customer(cid,dict(name="Legacy Business",customer_type="company",phone="0821234567"))
        after=raw_custom_fields_for(get_customer(cid))
        for key in ["whatsapp_number","contact_person_name","director_name","director_contact","contact_person_is_director"]:
            assert after[key]==raw[key]


def test_legacy_company_untouched_save_still_allowed(app):
    with app.app_context():
        cid=create_customer(dict(name="Legacy Company",customer_type="company"),notify=False)
        update_customer(cid,dict(name="Legacy Renamed",customer_type="company"))
        assert get_customer(cid)["name"]=="Legacy Renamed"


@pytest.mark.parametrize("field",["phone","director_name","director_contact"])
def test_staff_server_required_company_fields(app,client,field):
    owner(client); data=company(); data[field]=""
    response=client.post("/customers/new",data=data)
    assert response.status_code==200
    assert "is required" in response.get_data(as_text=True)
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM customers").fetchone()[0]==0


@pytest.mark.parametrize("width",[1440,390])
def test_staff_browser_toggle_and_save(app,client,width):
    owner(client)
    with app.app_context():
        cid=create_customer(dict(name="Browser Test",customer_type="company",phone="0821234567"),notify=False)
    server=make_server("127.0.0.1",0,app,threaded=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel="chrome")
            context=browser.new_context(viewport={"width":width,"height":1000})
            cookie=client.get_cookie("session")
            context.add_cookies([dict(name="session",value=cookie.value,domain="127.0.0.1",path="/")])
            page=context.new_page(); errors=[]
            page.on("pageerror",lambda e:errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.server_port}/customers/{cid}/edit")
            assert page.locator('[name="phone"]').evaluate("el=>el.required")
            assert page.locator('[name="director_name"]').is_visible()
            page.locator('[name="contact_person_name"]').fill("Browser Contact")
            page.locator('[name="contact_person_is_director"]').check()
            assert not page.locator('[name="director_name"]').is_visible()
            assert page.locator('[name="director_name"]').is_disabled()
            page.locator('[name="whatsapp_number"]').fill("0847654321")
            page.get_by_role("button",name="Save customer",exact=True).click()
            page.wait_for_url(f"**/customers/{cid}")
            page.goto(f"http://127.0.0.1:{server.server_port}/customers/{cid}/edit")
            assert page.locator('[name="contact_person_is_director"]').is_checked()
            assert page.locator('[name="contact_person_name"]').input_value()=="Browser Contact"
            assert page.locator('[name="whatsapp_number"]').input_value()=="0847654321"
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            shots=Path(os.environ["LOCALAPPDATA"])/"hermes/cache/scratch/ABI-341953299"
            shots.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(shots/f"staff-{width}.png"),full_page=True)
            page.locator('[name="contact_person_is_director"]').uncheck()
            assert page.locator('[name="director_name"]').evaluate("el=>el.required")
            page.locator('[name="customer_type"][value="individual"]').check()
            assert not page.locator('[name="director_name"]').is_visible()
            assert page.locator('[name="alternative_contact_name"]').is_visible()
            assert not errors
            browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        assert not thread.is_alive()
