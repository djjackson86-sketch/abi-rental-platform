"""Approved Sano storefront: shared ABI inventory, enquiry-only checkout."""
import secrets
from functools import wraps
from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, request, session, url_for
from app.db import get_db
from app.services import consent, csrf
from app.services.branches import list_branches
from app.services.cash import today_iso
from app.services.settings import get_company_settings
from app.services.reports import row_dict

bp=Blueprint('shop_front',__name__)


def public_enabled(view):
    @wraps(view)
    def wrapped(*args,**kwargs):
        settings=get_company_settings()
        if not settings['store_enabled']:
            response = make_response(render_template('public/store_unavailable.html',settings=settings))
            response.status_code = 200
            return response
        return view(*args,**kwargs)
    return wrapped


def catalogue_service():
    from app.services import shop_catalogue
    return shop_catalogue


def public_products(product_type, query=''):
    rows = catalogue_service().list_public_products(product_type=product_type,query=query)
    result=[];seen=set()
    category_copy = {row['name']: row['description'] or '' for row in get_db().execute('SELECT name,description FROM product_groups WHERE active=1 AND becomes_store_visible=1').fetchall()}
    for row in rows:
        row['category_description'] = category_copy.get(row.get('category_name'), '')
        units=tuple(sorted(int(unit['id']) for unit in row.get('model_units',[])))
        # Explicit operator-declared compatible models only; never merge same-name stock.
        group=units if len(units)>1 else (int(row['id']),)
        if group in seen:continue
        seen.add(group);result.append(row)
    return result


def context(area='home'):
    cart=session.get('shop_cart',{})
    if not isinstance(cart,dict):cart={}
    return dict(settings=get_company_settings(),shop_area=area,shop_cart_count=sum(int(q) for q in cart.values()),public_csrf=csrf.issue_token('shop_public'),branches=[row_dict(b) for b in list_branches(active_only=True)],notice_version=consent.current_notice_version() if consent.notice_is_publishable() else '',request_key=secrets.token_urlsafe(32),today=today_iso())


@public_enabled
def home():
    service=catalogue_service()
    defaults={'hire':'hire-move.jpg','buy':'sale-01-single-axle-unbraked-utility.jpg','repairs':'hire-06.png'}
    images={slot:service.home_image_url(slot) or url_for('static',filename='img/shop/'+name) for slot,name in defaults.items()}
    return render_template('public/shop_home.html',**context(),home_images=images)


@bp.get('/store/shop')
@public_enabled
def catalogue():
    q=request.args.get('q','').strip()[:120]
    return render_template('public/shop_catalogue.html',**context('shop'),products=public_products('sale',query=q),query=q)


@bp.get('/store/hire')
@public_enabled
def hire():
    try:selected=int(request.args.get('product_id',0))
    except (ValueError,TypeError):selected=0
    hire_products=public_products('rental')
    return render_template('public/shop_hire.html',**context('hire'),products=hire_products,selected_id=selected,category_notes=list(dict.fromkeys(p.get('category_description') for p in hire_products if p.get('category_description'))))


@bp.get('/store/repairs')
@public_enabled
def repairs():
    return render_template('public/shop_repairs.html',**context('repairs'),products=public_products('service'))


@public_enabled
def product_page(product_id):
    product=catalogue_service().get_public_product(product_id)
    if not product:abort(404)
    if product['product_type']=='rental':return redirect(url_for('shop_front.hire',product_id=product_id))
    if product['product_type']=='service':return redirect(url_for('shop_front.repairs',product_id=product_id))
    return render_template('public/shop_product.html',**context('shop'),product=product)


@bp.post('/store/cart/add/<int:product_id>')
@public_enabled
def add_to_cart(product_id):
    csrf.validate('shop_public')
    product=catalogue_service().get_public_product(product_id)
    if not product or product['product_type']!='sale':abort(404)
    try:quantity=int(request.form.get('quantity','1'))
    except (TypeError,ValueError):abort(400)
    if not 1<=quantity<=99:abort(400)
    cart=session.get('shop_cart',{})
    if not isinstance(cart,dict):cart={}
    if len(cart)>=30 and str(product_id) not in cart:abort(400)
    cart[str(product_id)]=min(99,int(cart.get(str(product_id),0))+quantity)
    session['shop_cart']=cart
    return redirect(url_for('shop_front.cart'))


@bp.post('/store/cart/remove/<int:product_id>')
@public_enabled
def remove_from_cart(product_id):
    csrf.validate('shop_public')
    cart=session.get('shop_cart',{})
    if not isinstance(cart,dict):cart={}
    cart.pop(str(product_id),None);session['shop_cart']=cart
    return redirect(url_for('shop_front.cart'))


@bp.get('/store/cart')
@public_enabled
def cart():
    stored=session.get('shop_cart',{})
    if not isinstance(stored,dict):stored={}
    lines=[];payload=[];clean={};total=0
    for product_id,quantity in list(stored.items())[:30]:
        try:
            product=catalogue_service().get_public_product(int(product_id));quantity=int(quantity)
        except (ValueError,TypeError):continue
        if not product or product['product_type']!='sale' or not 1<=quantity<=99:continue
        lines.append({'product':product,'quantity':quantity});payload.append({'product_id':product['id'],'quantity':quantity});clean[str(product['id'])]=quantity;total+=float(product['price_amount'] or 0)*quantity
    if clean!=stored:session['shop_cart']=clean
    return render_template('public/shop_cart.html',**context('shop'),cart_lines=lines,cart_payload=payload,cart_total=total)


@bp.after_request
def no_store(response):
    if response.mimetype=='text/html':response.headers['Cache-Control']='no-store, max-age=0'
    return response
