"""Full integrated storefront routing, inventory source and approved visual structure."""
import json,re
import pytest
from app import create_app
from app.db import get_db,now

@pytest.fixture
def app(tmp_path):
    app=create_app({'TESTING':True,'DATABASE':str(tmp_path/'shop.db'),'SECRET_KEY':'shop-tests','TURSO_DATABASE_URL':'','TURSO_AUTH_TOKEN':'','TELEGRAM_NOTIFICATIONS_ENABLED':'','VAPID_PUBLIC_KEY':'','VAPID_PRIVATE_KEY':''})
    with app.app_context():
        db=get_db()
        for pid,kind in [(501,'rental'),(502,'sale'),(503,'service')]:
            db.execute('INSERT INTO products (id,name,sku,product_type,price_amount,price_unit,security_deposit,quantity,tracking_method,active,public_visible,created_at) VALUES (?,?,?,?,?,?,?,3,\'bulk\',1,1,?)',(pid,'Public '+kind,'SHOP-'+kind,kind,225,'day' if kind=='rental' else 'fixed',500 if kind=='rental' else 0,now()))
        db.commit()
    return app

@pytest.fixture
def client(app):return app.test_client()

def csrf(client):
    client.get('/store/hire')
    with client.session_transaction() as s:return s['_csrf_tokens']['shop_public']

def test_home_exact_structure_no_footer_no_ribbon(client):
    r=client.get('/store');assert r.status_code==200
    s=r.get_data(as_text=True)
    assert 'What do you need today?' in s
    assert s.count('class="split-card"')==3
    for removed in ['<footer','class="hazard"','class="ribbon"','Hire · Sales · Repairs']:
        assert removed not in s
    assert '/static/img/shop/logo-wordmark.png' in s

@pytest.mark.parametrize('path',['/store/shop','/store/hire','/store/repairs','/store/cart','/store/products/502'])
def test_public_pages_render_and_no_footer(client,path):
    r=client.get(path);assert r.status_code==200
    assert '<footer' not in r.get_data(as_text=True)

def test_single_rental_page_has_customer_fields_and_actual_inventory(client):
    r=client.get('/store/hire');s=r.get_data(as_text=True)
    for marker in ['Public rental','name="customer_name"','name="customer_email"','name="customer_phone"','name="branch_id"','name="privacy_accepted"','name="start_date"','name="end_date"']:
        assert marker in s
    assert 'Public sale' not in s
    r=client.get('/store/products/501');assert r.status_code==302 and '/store/hire?product_id=501' in r.location

def test_cart_sale_only_csrf_and_hidden_stock_rechecked(app,client):
    assert client.post('/store/cart/add/502',data={'quantity':'2'}).status_code==400
    token=csrf(client)
    assert client.post('/store/cart/add/501',data={'quantity':'1','csrf_token':token}).status_code==404
    assert client.post('/store/cart/add/502',data={'quantity':'2','csrf_token':token}).status_code==302
    with client.session_transaction() as s:assert s['shop_cart']=={'502':2}
    assert 'Public sale' in client.get('/store/cart').get_data(as_text=True)
    with app.app_context():
        db=get_db();db.execute('UPDATE products SET public_visible=0 WHERE id=502');db.commit()
    assert 'Your cart is empty' in client.get('/store/cart').get_data(as_text=True)

def test_legacy_book_no_longer_writes_customers_or_orders(app,client):
    with app.app_context():before=[get_db().execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['customers','orders']]
    r=client.post('/store/products/501/book',data={'customer_name':'Legacy write','customer_email':'legacy@example.test'})
    assert r.status_code==303
    with app.app_context():assert before==[get_db().execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['customers','orders']]
    assert client.get('/store/booking/1').status_code==302

def test_global_closed_shop_denies_all_new_entrypoints(app,client):
    with app.app_context():
        db=get_db();db.execute('UPDATE company_settings SET store_enabled=0');db.commit()
    for path in ['/store','/store/shop','/store/hire','/store/repairs','/store/cart','/store/products/502']:
        response=client.get(path)
        assert response.status_code==200
        assert b'What do you need today?' not in response.data
