"""Scoped noncash correction. Dry-run default; no credentials or customer PII in git.

Use --credentials-file <restricted turso-access.txt> --backup-dir <restricted dir>.
Eight uniquely identified customers only. Theo Naicker is deliberately BLOCKED:
production has two exact-name customers, IDs 3302 and 3596. Admin must identify
which record is intended before a separate approval can include him.
Writes use one real SQLite/Turso transaction; no retry of writes.
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TICKET = 'ABI-341953292'
# exact customer ID, legacy row ID, source order, expected balance, exact name
TARGETS = [
    (1840, 30, '6048', 45.0, 'Robert Mokgobi'),
    (941, 174, '9733', 45.0, 'Neo Sepodumo'),
    (3709, 132, '6088', 35.0, 'Israel Msimango'),
    (2818, 70, '6452', 35.0, 'Vuyisile Malinga'),
    (2720, 117, '6749', 35.0, 'Nthangeni Mulovhedzi'),
    (4611, 160, '6831', 35.0, 'Collins legodi'),
    (2041, 56, '7149', 35.0, 'Lawrence Mathaba'),
    (1181, 97, '6425', 25.0, 'Archibold Mhlanga'),
]


def rows(db, sql, params=()):
    cursor = db.execute(sql, params)
    result = cursor.fetchall()
    return [dict(r) if hasattr(r, 'keys') else r.asdict() for r in result]


def financial_snapshot(db):
    # Full existing records, not only totals: no receipt, invoice, order or cash
    # data may be changed by this ticket. Digest privately, never print records.
    import hashlib
    tables = ['orders', 'documents', 'payments', 'legacy_balance_payments', 'cash_entries']
    existing = {r['name'] for r in rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: hashlib.sha256(json.dumps(rows(db, f'SELECT * FROM {t} ORDER BY id'),
                    sort_keys=True, default=str).encode()).hexdigest()
            for t in tables if t in existing}


def main():
    from flask import Flask, g
    from app.db import write_transaction
    from app.turso_db import connect_turso
    from app.services.legacy_balances import record_legacy_writeoff, legacy_balance_summary
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file', required=True)
    parser.add_argument('--backup-dir', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--rollback-test', action='store_true')
    args = parser.parse_args()
    text = Path(args.credentials_file).read_text()
    url = re.search(r'libsql://\S+', text).group()
    token = re.search(r'eyJ[\w.\-]+', text).group()
    db = connect_turso(url, token)
    app = Flask(__name__)
    destination = Path(args.backup_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    with app.app_context():
        g.db = db
        before_ledger = rows(db, 'SELECT * FROM legacy_customer_balances ORDER BY id')
        protected = financial_snapshot(db)
        result = []
        try:
            with write_transaction():
                tx = g.db
                for cid, rid, order, amount, name in TARGETS:
                    matches = rows(tx, 'SELECT id FROM customers WHERE lower(trim(name))=lower(?)', (name,))
                    assert len(matches) == 1 and matches[0]['id'] == cid, f'Ambiguous identity: {name}'
                    entry = rows(tx, 'SELECT * FROM legacy_customer_balances WHERE id=? AND customer_id=?', (rid, cid))
                    assert len(entry) == 1 and entry[0]['source_order_number'] == order, f'Wrong legacy row {rid}'
                    active = rows(tx, "SELECT id FROM legacy_customer_balances WHERE customer_id=? AND status='active' AND amount_due>settled_amount", (cid,))
                    assert {r['id'] for r in active} <= {rid}, f'Unexpected additional debt for {cid}'
                # Private snapshot exists BEFORE first write; transaction protects
                # expected amounts from concurrent receipts during the adjustment.
                snapshot = {'ticket': TICKET, 'stamp': stamp, 'legacy_rows': before_ledger,
                            'protected_financial_digests': protected, 'targets': TARGETS}
                (destination / f'{TICKET}-before-{stamp}.json').write_text(json.dumps(snapshot, indent=2))
                for cid, rid, order, amount, name in TARGETS:
                    result.append(record_legacy_writeoff(cid, {rid: amount}, TICKET,
                        'Admin-approved legacy debt write-off; no money received', '2026-11-04'))
                if not args.apply or args.rollback_test:
                    raise DryRunRollback()
        except DryRunRollback:
            print('ROLLBACK VERIFIED MODE: all attempted adjustments rolled back')
        after_ledger = rows(db, 'SELECT * FROM legacy_customer_balances ORDER BY id')
        assert financial_snapshot(db) == protected, 'Protected financial records changed'
        selected = {t[1] for t in TARGETS}
        assert [r for r in before_ledger if r['id'] not in selected] == [r for r in after_ledger if r['id'] not in selected]
        before_by_id = {r['id']: r for r in before_ledger}
        for row in after_ledger:
            original = before_by_id[row['id']]
            assert all(row[k] == v for k, v in original.items() if k not in ('status', 'updated_at'))
        if not args.apply or args.rollback_test:
            assert before_ledger == after_ledger, 'Rollback failed'
        summaries = []
        for cid, rid, order, amount, name in TARGETS:
            summary = legacy_balance_summary(cid)
            if args.apply and not args.rollback_test:
                assert summary['total'] == 0
                audit = rows(db, 'SELECT * FROM legacy_balance_writeoffs WHERE ticket_id=? AND legacy_balance_id=?', (TICKET, rid))
                assert len(audit) == 1 and audit[0]['amount'] == amount
            summaries.append({'id': cid, 'name': name, 'outstanding': summary['total'], 'writeoff': summary['writeoff_total']})
        report = {'applied': args.apply and not args.rollback_test, 'customers': summaries,
                  'newly_written_off': round(sum(r['amount'] for r in result), 2),
                  'protected_records_unchanged': True, 'unrelated_legacy_rows_unchanged': True,
                  'blocked': 'Theo Naicker: duplicate exact-name customer IDs 3302 and 3596'}
        (destination / f'{TICKET}-after-{stamp}.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        db.close()


class DryRunRollback(Exception):
    pass


if __name__ == '__main__':
    try:
        main()
    finally:
        # libsql-client sync worker may otherwise keep the process alive.
        import os
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(0)
