"""Approved removal of two incorrect imported balances; NEVER records payment.

Dry run by default. --apply requires --backup-dir outside the repository.
Uses an interactive write transaction, exact identities, durable full-row backup,
preconditions, and readback. Original amounts and receipt history are untouched.
To reverse, restore status/note/updated_at from the private before snapshot only
if the current row still matches the corresponding after snapshot.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TARGETS = {2533: 'Gordon Nzalo', 2495: 'Solomon Nenzhelele'}
NOTE = 'ABI-341953282: incorrect imported balance written off per admin approval; no money received.'


def rows(db, sql, params=()):
    cursor = db.execute(sql, params)
    values = cursor.fetchall()
    return [dict(r) if hasattr(r, 'keys') else r.asdict() for r in values]


def snapshot(db):
    tables = [r['name'] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table'")]
    protected = [t for t in tables if t in ('payments', 'legacy_balance_payments', 'orders') or 'cash' in t]
    hashes = {}
    for table in protected:
        data = rows(db, 'SELECT * FROM "' + table + '" ORDER BY rowid')
        hashes[table] = {'count': len(data), 'sha256': hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()}
    return {'legacy': rows(db, 'SELECT * FROM legacy_customer_balances ORDER BY id'), 'protected': hashes}


def correct(db, backup_dir=None):
    for cid, name in TARGETS.items():
        matches = rows(db, 'SELECT id,name FROM customers WHERE LOWER(TRIM(name))=LOWER(?)', (name,))
        if matches != [{'id': cid, 'name': name}]:
            raise ValueError('Ambiguous or changed identity: ' + name)
    before = snapshot(db)
    affected = [r for r in before['legacy'] if r['customer_id'] in TARGETS and r['status'] == 'active' and float(r['amount_due'] or 0) > float(r['settled_amount'] or 0)]
    if any(r['source_system'] != 'booqable' for r in affected):
        raise ValueError('Unexpected source system')
    print(json.dumps({'customers': TARGETS, 'rows': [{k:r[k] for k in ('id','customer_id','source_order_number','amount_due','settled_amount','status')} for r in affected]}, indent=2))
    if backup_dir is None:
        print('DRY RUN: no changes')
        return
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    (directory / ('before-' + stamp + '.json')).write_text(json.dumps(before, indent=2, default=str), encoding='utf-8')
    for row in affected:
        note = (row['note'] or '') + '\n' + NOTE
        db.execute("UPDATE legacy_customer_balances SET status='waived', note=?, updated_at=? WHERE id=? AND status='active' AND amount_due=? AND settled_amount=?", (note, datetime.now(timezone.utc).isoformat(), row['id'], row['amount_due'], row['settled_amount']))
    after = snapshot(db)
    assert before['protected'] == after['protected'], 'Financial history changed'
    affected_ids = {r['id'] for r in affected}
    assert [r for r in before['legacy'] if r['id'] not in affected_ids] == [r for r in after['legacy'] if r['id'] not in affected_ids], 'Unrelated legacy changed'
    by_id = {r['id']: r for r in after['legacy']}
    for row in affected:
        expected = dict(row, status='waived', note=(row['note'] or '') + '\n' + NOTE, updated_at=by_id[row['id']]['updated_at'])
        assert by_id[row['id']] == expected
    for cid in TARGETS:
        assert not [r for r in after['legacy'] if r['customer_id'] == cid and r['status'] == 'active' and float(r['amount_due']) > float(r['settled_amount'])]
    (directory / ('after-' + stamp + '.json')).write_text(json.dumps(after, indent=2, default=str), encoding='utf-8')
    print('Verified: %d rows waived; both outstanding totals zero; receipts/orders/cash and unrelated legacy unchanged.' % len(affected))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir')
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error('--backup-dir required for apply')
    credentials = Path('E:/Hermes/abi-rental-platform/turso-access.txt').read_text()
    url = re.search(r'(libsql://\S+)', credentials).group(1)
    token = re.search(r'(eyJ[\w.\-]+)', credentials).group(1)
    from app.turso_db import connect_turso
    db = connect_turso(url, token)
    try:
        if args.apply:
            with db.write_transaction() as transaction:
                correct(transaction, args.backup_dir)
            print('COMMITTED')
        else:
            correct(db)
        # Exact post-commit readback, from a separate request.
        print(rows(db, "SELECT customer_id, ROUND(SUM(CASE WHEN status='active' THEN MAX(amount_due-settled_amount,0) ELSE 0 END),2) AS outstanding FROM legacy_customer_balances WHERE customer_id IN (2495,2533) GROUP BY customer_id"))
    finally:
        db.close()

if __name__ == '__main__':
    main()
