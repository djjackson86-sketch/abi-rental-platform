"""Shared scope and totals for customer-level receipts, without order double-payment."""
from app.db import get_db
from app.services.access import customer_branch_clause


def receipt_where(table, include_archived=False, branch_id=None, date_from='', date_to=''):
    from app.services.payments import normalise_payment_date_filter
    where=["1=1" if include_archived else "r.status='paid' AND r.deleted_at=''"]
    scope,params=customer_branch_clause('c',branch_id=branch_id)
    if scope:
        where.append(scope.removeprefix(' AND ').replace('c.branch_id','COALESCE(r.branch_id,c.branch_id)'))
    for value,op in ((date_from,'>='),(date_to,'<=')):
        day=normalise_payment_date_filter(value)
        if day:
            where.append(f"substr(COALESCE(NULLIF(r.payment_date,''),r.created_at),1,10) {op} ?")
            params.append(day)
    return where,params


def receipt_totals(table='account_repayments', branch_id=None, date_from='', date_to='', include_archived=False):
    if table not in ('account_repayments','prepaid_fundings'):
        raise ValueError('Unsupported receipt table')
    where,params=receipt_where(table,include_archived,branch_id,date_from,date_to)
    rows=get_db().execute(f"SELECT r.method,SUM(r.amount) AS total FROM {table} r JOIN customers c ON c.id=r.customer_id WHERE {' AND '.join(where)} GROUP BY r.method",params).fetchall()
    return {r['method']:round(float(r['total'] or 0),2) for r in rows}


def receipt_ledger_sql(table, where):
    if table not in ('account_repayments','prepaid_fundings'):
        raise ValueError('Unsupported receipt table')
    label='Account repayment' if table=='account_repayments' else 'Prepaid funding (not revenue)'
    return f"""SELECT NULL AS id,NULL AS order_id,r.amount,r.method,r.reference,r.status,r.payment_date,r.created_at,r.deleted_at,
      '{table}' AS synthetic_kind,'{label}' AS order_number,COALESCE(r.branch_id,c.branch_id) AS collect_branch_id,
      NULL AS return_branch_id,c.name AS customer_name,b.name AS collect_branch_name,NULL AS return_branch_name,
      COALESCE(b.name,'Unassigned') AS branch_label,c.id AS customer_id,r.id AS legacy_receipt_id
      FROM {table} r JOIN customers c ON c.id=r.customer_id LEFT JOIN branches b ON b.id=COALESCE(r.branch_id,c.branch_id)
      WHERE {' AND '.join(where)}"""
