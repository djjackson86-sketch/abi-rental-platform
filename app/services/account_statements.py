"""Explicit facility activity using the existing customer-statement layout."""
from app.db import get_db
from app.services.customers import get_customer, customer_statement
from app.services.payments import normalise_payment_date_filter
from app.services.credit_limits import facility_summary


def account_statement(customer_id, date_from='', date_to=''):
    customer = get_customer(customer_id)  # existing branch/access scope
    if not customer or not customer['credit_allowed']:
        return None
    start = normalise_payment_date_filter(date_from)
    end = normalise_payment_date_filter(date_to)
    if start and end and start > end:
        start, end = end, start
    view = customer_statement(customer_id, start, end)
    rows = get_db().execute("""SELECT p.*, o.total, o.order_number
        FROM payments p JOIN orders o ON o.id=p.order_id
        WHERE o.customer_id=? AND COALESCE(p.status, 'paid')='paid'
        AND COALESCE(p.deleted_at, '')=''
        AND EXISTS (SELECT 1 FROM payments a WHERE a.order_id=o.id AND a.method='account')
        ORDER BY substr(COALESCE(NULLIF(p.payment_date, ''),p.created_at),1,10),p.id""", (customer_id,)).fetchall()
    allocated, receipts = {}, {}
    activity = []
    opening = charges = repayments = 0.0
    for row in rows:
        oid = row['order_id']
        before = min(max(allocated.get(oid, 0), 0), max(float(row['total'])-receipts.get(oid, 0), 0))
        if row['method'] == 'account':
            allocated[oid] = allocated.get(oid, 0) + float(row['amount'])
        else:
            receipts[oid] = receipts.get(oid, 0) + float(row['amount'])
        after = min(max(allocated.get(oid, 0), 0), max(float(row['total'])-receipts.get(oid, 0), 0))
        delta = round(after-before, 2)
        day = str(row['payment_date'] or row['created_at'])[:10]
        if start and day < start:
            opening = round(opening + delta, 2)
        elif (not end or day <= end) and delta:
            charges += max(delta, 0)
            repayments += max(-delta, 0)
            activity.append({'date': day, 'type': 'Account' if row['method']=='account' else 'Repayment' if delta<0 else 'Reversal',
                'detail': f"{row['order_number']} - {row['method']} {row['reference'] or ''}",
                'amount': delta, 'amount_display': f'R{delta:.2f}'})
    closing = round(opening+charges-repayments, 2)
    facility = facility_summary(customer_id)
    view.update(title='ACCOUNT STATEMENT', statement_number=f'ACCOUNT-STATEMENT-{customer_id}', activity=activity,
        activity_count=len(activity), facility=facility,
        summary_rows=[('Opening balance',f'R{opening:.2f}',False),('Account charges',f'R{charges:.2f}',False),
            ('Repayments',f'R{repayments:.2f}',False),('Closing balance',f'R{closing:.2f}',True),
            ('Configured credit limit',f"R{facility['limit']:.2f}",False),
            ('Available credit (current)',f"R{facility['available']:.2f}",True)])
    return view
