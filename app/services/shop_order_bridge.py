"""Staff-controlled handoff; an enquiry becomes an order only on normal draft save."""
from flask import abort,session
from app.db import get_db, write_transaction
from app.routes.shop_enquiries import _live_account,_admin_enquiry
from app.services import shop_enquiries
from app.services.orders import create_order
from app.services.reports import row_dict


def prefill(enquiry_id,customer_id=None,products=()):
    account=_live_account()
    enquiry=_admin_enquiry(enquiry_id,account)
    data=shop_enquiries.order_prefill(enquiry_id,customer_id)
    data['linked_order_id']=enquiry.get('linked_order_id')
    data['booking_type']='return'
    by_id={int(p['id']):p for p in products}
    for line in data['lines']:
        p=by_id.get(int(line['product_id'] or 0))
        if p is None:
            from app.services.shop_catalogue import get_product
            p = get_product(line['product_id'])
        p=row_dict(p) if p is not None else None
        if not p or not int(p.get('active',1)) or int(p.get('under_maintenance') or 0) or not shop_enquiries._product_at_branch(p, enquiry['branch_id']):
            raise ValueError('An enquired product is no longer available to this branch. Review the enquiry before preparing the order.')
        line['product_display']=str(p['name'])+(' — '+str(p['sku']) if p['sku'] else '')+(' — '+str(p['branch_name']) if p['branch_name'] else '')
        # Normal order services recalculate from current inventory, never a public/snapshot price.
        line['custom_unit_price']=''
        line['description']=p['description'] or ''
    return data


def create_linked_order(form,enquiry_id):
    account=_live_account()
    # Serialised on Turso with BEGIN IMMEDIATE; every customer/order write and the
    # final link share one scope. A failed link rolls the new draft back.
    with write_transaction():
        enquiry=_admin_enquiry(enquiry_id,account)
        if enquiry.get('linked_order_id'):
            return int(enquiry['linked_order_id'])
        if int(form.get('collect_branch_id') or 0)!=int(enquiry['branch_id']):
            raise ValueError('Keep the collection branch from this enquiry. Create an unrelated order separately.')
        if not form.get('customer_id'):
            raise ValueError('Attach or create the matching customer before converting this enquiry.')
        order_id=create_order(form,notify=False)
        shop_enquiries.link_order(enquiry_id,order_id,user_id=account['id'])
        return order_id
