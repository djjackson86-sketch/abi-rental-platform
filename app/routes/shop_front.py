"""Approved Sano storefront: shared ABI inventory, enquiry-only checkout."""
import secrets
from functools import wraps
from flask import Blueprint, abort, current_app, flash, jsonify, make_response, redirect, render_template, request, session, url_for
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
    return _dedupe_cards(rows)


def _dedupe_cards(rows):
    """One card per explicit model group (defensive; pagination already does it)."""
    result=[];seen=set()
    for row in rows:
        units=tuple(sorted(int(unit['id']) for unit in row.get('model_units',[])))
        # Explicit operator-declared compatible models only; never merge same-name stock.
        group=units if len(units)>1 else (int(row['id']),)
        if group in seen:continue
        seen.add(group);result.append(row)
    return result


def category_descriptions():
    """Category copy for the hire page, in one small query."""
    return {row['name']: row['description'] or '' for row in get_db().execute('SELECT name,description FROM product_groups WHERE active=1 AND becomes_store_visible=1').fetchall()}


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
    """Bounded, server-paginated sale catalogue.

    ``format=fragment`` returns just the cards and the next URL as JSON for the
    progressive "Load more" control; without JavaScript the same links are
    ordinary page links, so every item stays reachable.
    """
    q=request.args.get('q','').strip()[:120]
    category=request.args.get('category','').strip()[:80]
    result=catalogue_service().page_public_products(product_type='sale',query=q,category=category,page=request.args.get('page','1'))
    result['items']=_dedupe_cards(result['items'])
    next_url=(url_for('shop_front.catalogue',q=q or None,category=category or None,page=result['next_page']) if result['has_next'] else '')
    prev_url=(url_for('shop_front.catalogue',q=q or None,category=category or None,page=result['prev_page']) if result['has_prev'] else '')
    if request.args.get('format')=='fragment':
        html=render_template('public/_shop_cards.html',products=result['items'])
        return jsonify(html=html,next_url=next_url,prev_url=prev_url,total=result['total'],page=result['page'],pages=result['pages'])
    categories=[dict(id=row['id'],name=row['name']) for row in get_db().execute(
        "SELECT DISTINCT g.id,g.name FROM product_groups g JOIN products p ON p.product_group_id=g.id WHERE g.active=1 AND g.becomes_store_visible=1 AND p.active=1 AND p.public_visible=1 AND COALESCE(p.under_maintenance,0)=0 AND p.product_type='sale' ORDER BY CASE WHEN LOWER(g.name) LIKE '%trailer%' THEN 0 ELSE 1 END,g.name").fetchall()]
    return render_template('public/shop_catalogue.html',**context('shop'),products=result['items'],pagination=result,query=q,category=category,categories=categories,next_url=next_url,prev_url=prev_url)


@bp.get('/store/hire')
@public_enabled
def hire():
    try:selected=int(request.args.get('product_id',0))
    except (ValueError,TypeError):selected=0
    hire_products=public_products('rental')
    category_copy=category_descriptions()
    for product in hire_products:
        product['category_description']=category_copy.get(product.get('category_name'),'')
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
