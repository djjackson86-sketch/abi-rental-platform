"""Integrated handoff and public-contract regressions on the real app factory."""
import re
import pytest
from test_shop_enquiries import app,client,reset_enquiry_limits,add_branch,add_product,publish_notice,rental_form,login
from app.db import get_db
from app.services import customers,shop_enquiries


def setup_enquiry(app):
    branch=add_branch(app,'Integration depot');product=add_product(app,'Integration luggage',branch_id=branch,sku='INT-1');publish_notice(app)
    customer=customers.create_customer({'customer_type':'individual','name':'Thandi Mokoena','email':'thandi@example.com','phone':'0825550101'},branch_id=branch,notify=False)
    form=rental_form(branch,product,request_key='integration-key')
    enquiry=shop_enquiries.submit_enquiry(form,notify=False)
    return branch,product,customer,form,enquiry['enquiry_id']


def test_order_handoff_prefills_exact_picker_labels_and_links_once(app,client):
    branch,product,customer,public_form,eid=setup_enquiry(app)
    login(client,1,role='owner',branch_id=branch,can_view_all=True)
    page=client.get('/orders/new?enquiry_id='+str(eid));assert page.status_code==200
    html=page.get_data(as_text=True)
    assert f'name="customer_id" id="customer-id" value="{customer}"' in html
    assert 'Integration luggage — INT-1' in html
    assert 'name="shop_enquiry_id"' in html
    with client.session_transaction() as s:token=s['_csrf_tokens']['shop_enquiry_convert']
    data={'shop_enquiry_id':str(eid),'csrf_token':token,'customer_id':str(customer),'booking_type':'return','collect_branch_id':str(branch),'return_branch_id':str(branch),'product_id':str(product),'quantity':'2','custom_name':'','custom_unit_price':'','custom_billing_mode':'rental_day','start_date':public_form['start_date'],'start_time':'09:00','end_date':public_form['end_date'],'end_time':'15:00'}
    before=get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0]
    r=client.post('/orders/new?enquiry_id='+str(eid),data=data);assert r.status_code==302
    assert re.search(r'/orders/\d+$',r.location),r.location
    assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0]==before+1
    saved=shop_enquiries.get_enquiry(eid);assert saved['status']=='converted' and saved['linked_order_id']
    replay=client.post('/orders/new?enquiry_id='+str(eid),data=data)
    assert replay.location==r.location
    assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0]==before+1


def test_handoff_cannot_write_without_csrf_or_access(app,client):
    branch,product,customer,form,eid=setup_enquiry(app)
    login(client,1,role='owner',branch_id=branch,can_view_all=True)
    assert client.post('/orders/new?enquiry_id='+str(eid),data={'customer_id':str(customer)}).status_code==400
    assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0


def test_same_request_key_with_changed_payload_is_refused(app):
    branch,product,customer,form,eid=setup_enquiry(app)
    form['notes']='A changed payload may not reuse a completed key'
    with pytest.raises(ValueError,match='different details'):shop_enquiries.submit_enquiry(form,notify=False)
    assert get_db().execute('SELECT COUNT(*) FROM shop_enquiries').fetchone()[0]==1


def test_general_repairs_without_inventory_service_are_accepted(app):
    branch=add_branch(app,'Repair depot');publish_notice(app)
    form={'enquiry_type':'repair','branch_id':str(branch),'customer_name':'Repair Person','customer_email':'repair@example.test','customer_phone':'0825550123','notes':'The trailer brake lights are not working.','privacy_accepted':'1','notice_version':'1.1','request_key':'repair-general'}
    result=shop_enquiries.submit_enquiry(form,notify=False)
    assert shop_enquiries.get_enquiry(result['enquiry_id'])['items']==[]
    assert get_db().execute('SELECT COUNT(*) FROM orders').fetchone()[0]==0


def test_product_types_cannot_cross_journeys(app):
    branch=add_branch(app,'Type depot');product=add_product(app,'Hire only',branch_id=branch);publish_notice(app)
    form=rental_form(branch,product,enquiry_type='purchase',request_key='wrong-type')
    with pytest.raises(ValueError,match='own product types'):shop_enquiries.submit_enquiry(form,notify=False)


def test_real_libsql_rows_roundtrip_enquiry_and_render(app,client):
    from libsql_client.result import Row
    branch=add_branch(app,'Libsql depot');product=add_product(app,'Libsql trailer',branch_id=branch);publish_notice(app)
    db=get_db()
    db.row_factory=lambda cursor,values:Row({column[0]:i for i,column in enumerate(cursor.description)},tuple(values))
    result=shop_enquiries.submit_enquiry(rental_form(branch,product,request_key='libsql-row'),notify=False)
    assert shop_enquiries.get_enquiry(result['enquiry_id'])['customer_name']=='Thandi Mokoena'
    login(client,1,role='owner',branch_id=branch,can_view_all=True)
    assert client.get('/online-store/enquiries/'+str(result['enquiry_id'])).status_code==200
    assert client.get('/online-store/listings').status_code==200
    assert client.get('/store/hire').status_code==200


def test_explicit_model_one_public_listing_routes_to_branch_unit(app,client):
    from app.services import shop_catalogue
    first_branch=add_branch(app,'Model first');second_branch=add_branch(app,'Model second')
    first=add_product(app,'Model first unit',branch_id=first_branch,sku='MODEL-1')
    second=add_product(app,'Model second unit',branch_id=second_branch,sku='MODEL-2')
    shop_catalogue.save_listing(first,{'model_key':'same-model','public_visible':'1'})
    shop_catalogue.save_listing(second,{'model_key':'same-model','public_visible':'1'})
    publish_notice(app)
    html=client.get('/store/hire').get_data(as_text=True)
    assert html.count('data-rate=')==1
    result=shop_enquiries.submit_enquiry(rental_form(second_branch,first,request_key='model-other-branch'),notify=False)
    enquiry=shop_enquiries.get_enquiry(result['enquiry_id'])
    assert enquiry['branch_id']==second_branch and enquiry['items'][0]['product_id']==second
    assert get_db().execute('SELECT COUNT(*) FROM products').fetchone()[0]==2


def test_rendered_honeypot_is_checked_by_submit(app,client):
    branch=add_branch(app,'Trap depot');product=add_product(app,'Trap trailer',branch_id=branch);publish_notice(app)
    page=client.get('/store/hire').get_data(as_text=True)
    assert 'name="company_website"' in page
    with client.session_transaction() as s:token=s['_csrf_tokens']['shop_public']
    form=rental_form(branch,product,company_website='https://spam.example',csrf_token=token)
    response=client.post('/store/enquiries/request',data=form,headers={'Accept':'application/json'})
    assert response.status_code==400
    assert get_db().execute('SELECT COUNT(*) FROM shop_enquiries').fetchone()[0]==0


def test_unsaved_listing_preview_does_not_publish_or_mutate(app,client):
    branch=add_branch(app,'Preview depot');product=add_product(app,'Original listing',branch_id=branch)
    login(client,1,role='owner',branch_id=branch,can_view_all=True)
    client.get('/online-store/listings/'+str(product))
    with client.session_transaction() as s:token=s['_csrf_tokens']['shop_admin']
    response=client.post(f'/online-store/listings/{product}/preview',data={'csrf_token':token,'store_title':'Unsaved proposed title','store_description':'Draft preview only','public_visible':'0'})
    assert response.status_code==200 and 'Unsaved proposed title' in response.get_data(as_text=True)
    assert 'Add to cart →' not in response.get_data(as_text=True)
    assert get_db().execute('SELECT public_visible FROM products WHERE id=?',(product,)).fetchone()[0]==1
    assert get_db().execute('SELECT COUNT(*) FROM shop_listing_meta WHERE product_id=?',(product,)).fetchone()[0]==0


def test_listing_form_unpublish_posts_explicit_zero(app,client):
    branch=add_branch(app,'Hide depot');product=add_product(app,'Live listing',branch_id=branch)
    login(client,1,role='owner',branch_id=branch,can_view_all=True)
    html=client.get(f'/online-store/listings/{product}').get_data(as_text=True)
    assert '<input type="hidden" name="public_visible" value="0">' in html
    with client.session_transaction() as s:token=s['_csrf_tokens']['shop_admin']
    assert client.post(f'/online-store/listings/{product}',data={'csrf_token':token,'public_visible':'0'}).status_code==302
    assert get_db().execute('SELECT public_visible FROM products WHERE id=?',(product,)).fetchone()[0]==0


def test_enquiry_can_prepare_stock_allocated_from_another_owner_branch(app,client):
    from test_shop_enquiries import add_user
    owner_branch=add_branch(app,'Owning depot');stock_branch=add_branch(app,'Stocked depot')
    product=add_product(app,'Cross-owned bulk stock',branch_id=owner_branch,sku='CROSS-STOCK')
    get_db().execute('INSERT INTO product_branch_stock (product_id,branch_id,quantity,updated_at) VALUES (?,?,3,?)',(product,stock_branch,'2026-01-01'))
    get_db().commit();publish_notice(app)
    result=shop_enquiries.submit_enquiry(rental_form(stock_branch,product,request_key='cross-owned'),notify=False)
    user=add_user(app,'Stocked depot staff',branch_id=stock_branch,modules=['orders','new_order','customers'])
    login(client,user,branch_id=stock_branch,modules=['orders','new_order','customers'],branch_ids=[stock_branch])
    response=client.get('/orders/new?enquiry_id='+str(result['enquiry_id']))
    assert response.status_code==200
    html=response.get_data(as_text=True)
    assert 'Cross-owned bulk stock — CROSS-STOCK' in html
    assert f'class="product-id" value="{product}"' in html


def test_trusted_proxy_only_uses_one_forwarded_ip_hop_not_forwarded_host(tmp_path):
    from app import create_app
    from flask import request,jsonify
    proxy_app=create_app({'TESTING':True,'DATABASE':str(tmp_path/'proxy.sqlite'),'TURSO_DATABASE_URL':'','TURSO_AUTH_TOKEN':'','TRUST_PROXY_HEADERS':True})
    @proxy_app.get('/_test/proxy')
    def proxy_probe():return jsonify(ip=request.remote_addr,host=request.host,scheme=request.scheme)
    result=proxy_app.test_client().get('/_test/proxy',headers={'X-Forwarded-For':'192.0.2.66, 198.51.100.7','X-Forwarded-Proto':'https','X-Forwarded-Host':'evil.example'}).get_json()
    assert result=={'ip':'198.51.100.7','host':'localhost','scheme':'https'}
