"""Read-only statements of the existing explicit Account facility."""
from app.db import get_db
from app.services.credit_limits import facility_summary, _exposure_sql, _opening_sql
from app.services.customers import get_customer
from app.services.customer_credits import customer_credit_balance
from app.services.payments import normalise_payment_date_filter


def account_statement(customer_id, date_from='', date_to=''):
    customer=get_customer(customer_id)
    if not customer:
        return None
    db=get_db()
    date_from=normalise_payment_date_filter(date_from)
    date_to=normalise_payment_date_filter(date_to)
    if date_from and date_to and date_from>date_to:
        date_from,date_to=date_to,date_from
    facility=facility_summary(customer_id)
    orders=db.execute(f"""SELECT o.id,o.order_number,o.total,o.created_at,
        {_opening_sql()} AS opening_exposure, {_exposure_sql()} AS outstanding,
        COALESCE((SELECT SUM(p.amount) FROM payments p WHERE p.order_id=o.id AND p.method='account'
          AND p.status='paid' AND COALESCE(p.deleted_at,'')=''),0) AS account_allocated
        FROM orders o WHERE o.customer_id=? AND ({_opening_sql()}>0 OR EXISTS(
          SELECT 1 FROM payments p WHERE p.order_id=o.id AND p.method='account'))
        ORDER BY o.created_at,o.id""",(customer_id,)).fetchall()
    charges=[]
    activity=[]
    for order in orders:
        charges.append({'order_id':order['id'],'order_number':order['order_number'],
            'allocated':max(float(order['opening_exposure']),float(order['account_allocated'])),
            'outstanding':float(order['outstanding']), 'total':float(order['total'])})
        if order['opening_exposure']:
            activity.append({'date':str(order['created_at'])[:10], 'type':'Opening Account exposure',
                'detail':order['order_number'],'amount':float(order['opening_exposure']), 'id':-order['id']})
        rows=db.execute("""SELECT * FROM payments WHERE order_id=? AND status='paid'
            AND COALESCE(deleted_at,'')='' ORDER BY COALESCE(NULLIF(payment_date,''),created_at),id""",(order['id'],)).fetchall()
        for row in rows:
            activity.append({'date':str(row['payment_date'] or row['created_at'])[:10],
                'type':'Account allocation' if row['method']=='account' else 'Order receipt / repayment',
                'detail':order['order_number']+' · '+row['method'].replace('_',' ').title()+' · '+(row['reference'] or ''),
                'amount':float(row['amount']), 'id':row['id']})
    activity.sort(key=lambda r:(r['date'],r['id']))
    activity=[r for r in activity if (not date_from or r['date']>=date_from) and (not date_to or r['date']<=date_to)]
    return {'customer':customer, 'facility':facility, 'orders':charges, 'activity':activity,
        'prepaid_balance':customer_credit_balance(customer_id),'date_from':date_from,'date_to':date_to}


def account_statement_lines(view):
    c=view['customer']; f=view['facility']
    lines=['ACCOUNT STATEMENT',c['name'],
        'Allocated credit: R%.2f' % f['limit'], 'Credit usage: R%.2f' % f['used'],
        'Available credit: R%.2f' % f['available'],
        'Prepaid customer credit (separate): R%.2f' % view['prepaid_balance'],
        'Current balances; activity dates: '+(view['date_from'] or 'All')+' to '+(view['date_to'] or 'All'),
        '', 'ORDERS / ACCOUNT USAGE']
    for row in view['orders']:
        lines.append('%s | Allocated R%.2f | Outstanding R%.2f' % (row['order_number'],row['allocated'],row['outstanding']))
    lines+=['','DATED ACTIVITY']
    for row in view['activity']:
        lines.append('%s | %s | R%.2f' % (row['date'],row['type'],row['amount']))
        lines.append('  '+row['detail'])
    if not view['activity']:
        lines.append('No Account activity in this period')
    lines+=['','Account allocations settle orders but are not revenue.',
        'Cash/card/EFT receipts recognise revenue on their payment date.',
        'Receipts are allocated to their recorded order, never counted twice.',
        'Unused prepaid funds are not revenue; application to an order is revenue.']
    return lines
