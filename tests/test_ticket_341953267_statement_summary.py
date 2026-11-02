"""Ticket ABI-341953267: the customer statement's totals box is a balance box.

The client asked for the three component lines under the totals box —
`Invoices`, `Legacy balances` and `Payments` — to be removed. They are the
statement's *internal* build-up of the balance; the box now carries only the
balance line(s): `Balance outstanding` on a full-history statement, and
`Opening balance` + `Closing balance` on a ranged one.

What must NOT change, and is pinned here:
  * the figures themselves (`invoiced_total`, `legacy_total`, `paid_total` and
    their `*_display` twins) stay on the statement view dict — other screens and
    the API read them;
  * the activity table still prints every invoice, receipt and imported debt row
    with its amount, so nothing disappears from the statement itself;
  * the credit-facility rows (Credit limit / used / available) are a different
    block added by ticket ABI-341953166 and stay;
  * the account/facility statement (`account_statements.py`) supplies its own
    `summary_rows` and is not touched by this change;
  * the box still fits above the page footer now that it is shorter.
"""
import re

from test_ticket_341953085_customer_statement import (
    app, client, login, pdf_source, seed_customer, seed_ticket_ledger,
)
from app.db import get_db
from app.services.customers import customer_statement
from app.services.legacy_balances import record_legacy_payment, upsert_legacy_balance
from app.services.pdf_documents import (
    STATEMENT_FOOTER_Y,
    STATEMENT_SUMMARY_WIDTH,
    STATEMENT_SUMMARY_X,
    _statement_summary_rows,
    customer_statement_pdf_bytes,
)


COMPONENT_LABELS = ("(Invoices)", "(Legacy balances)", "(Payments)")


def _summary_box(source):
    """The totals-box rectangle, straight off the page stream."""
    pattern = re.compile(
        rf'{STATEMENT_SUMMARY_X:.2f} (-?[\d.]+) {STATEMENT_SUMMARY_WIDTH:.2f} ([\d.]+) re')
    matches = pattern.findall(source)
    assert len(matches) == 1, f'expected one totals box, found {len(matches)}'
    bottom, height = float(matches[0][0]), float(matches[0][1])
    return bottom, bottom + height


def _segment_adds_legacy_balance(db, customer_id, reference='OLD-267', amount=400):
    upsert_legacy_balance(customer_id, 'old', reference, amount, source_total=9000,
                          source_paid=8000, source_started_at='2026-08-01',
                          source_created_at='')
    db.execute("UPDATE legacy_customer_balances SET created_at='2026-09-01T12:00:00' "
               "WHERE source_order_number=?", (reference,))
    db.commit()


# --- the summary rows themselves ------------------------------------------------


def test_full_history_summary_rows_are_the_balance_line_only(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()
        view = customer_statement(customer_id)

    assert _statement_summary_rows(view) == [('Balance outstanding', 'R275.00', True)]
    # The component figures are untouched on the view (only their box rows went).
    assert (view['invoiced_total'], view['paid_total']) == (1725.0, 1450.0)
    assert (view['invoiced_total_display'], view['paid_total_display']) == ('R1725.00', 'R1450.00')


def test_ranged_summary_rows_keep_the_opening_and_closing_balance(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()
        view = customer_statement(customer_id, date_from='2026-08-12', date_to='')

    assert _statement_summary_rows(view) == [
        ('Opening balance', 'R1150.00', False),
        ('Closing balance', 'R275.00', True),
    ]


def test_legacy_and_facility_rows_are_not_components(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_ticket_ledger(db)
        _segment_adds_legacy_balance(db, customer_id)
        legacy_view = customer_statement(customer_id)

    # A legacy total no longer prints as a row, even though the ledger has one.
    assert legacy_view['legacy_balance_count'] == 1
    assert legacy_view['legacy_total'] == 400.0
    assert [label for label, _, _ in _statement_summary_rows(legacy_view)] == ['Balance outstanding']


def test_account_statement_summary_rows_pass_through_untouched():
    """`account_statements.py` hands in its own rows; they must not be filtered."""
    rows = [('Account charges', 'R100.00', False), ('Repayments', 'R30.00', False),
            ('Credit available', 'R870.00', True)]
    assert _statement_summary_rows({'summary_rows': rows, 'date_from': '2026-08-01'}) == rows


# --- the rendered PDF -----------------------------------------------------------


def test_full_history_pdf_box_prints_only_the_balance(client, app):
    login(client)
    with app.app_context():
        db = get_db()
        customer_id = seed_ticket_ledger(db)
        _segment_adds_legacy_balance(db, customer_id)
        view = customer_statement(customer_id)
        source = pdf_source(customer_statement_pdf_bytes(view))

    for label in COMPONENT_LABELS:
        assert label not in source
    assert '(Balance outstanding)' in source
    assert '(R675.00)' in source          # 1725 invoiced + 400 legacy - 1450 paid
    assert '(Opening balance)' not in source and '(Closing balance)' not in source
    # The activity table still carries the components as rows, with their money.
    for token in ('INV-90001', 'INV-90002', 'EFT ref 88', 'OLD-267'):
        assert token in source
    assert '(R1150.00)' in source and '(-R300.00)' in source


def test_ranged_pdf_box_prints_opening_and_closing_only(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        get_db().commit()
        view = customer_statement(customer_id, date_from='2026-08-12', date_to='')
        source = pdf_source(customer_statement_pdf_bytes(view))

    for label in COMPONENT_LABELS:
        assert label not in source
    assert '(Opening balance)' in source and '(Closing balance)' in source
    assert '(Balance outstanding)' not in source
    assert '(R1150.00)' in source and '(R275.00)' in source


def test_shorter_box_still_clears_the_page_footer(client, app):
    login(client)
    with app.app_context():
        customer_id = seed_ticket_ledger(get_db())
        for index in range(40):
            # A long ledger pushes the activity table near the bottom of a page.
            from test_ticket_341953085_customer_statement import seed_invoice, seed_order
            db = get_db()
            order_id = seed_order(db, customer_id, f'ORD-BOX-{index:03d}', 100.0,
                                  created_at=f'2026-10-{1 + (index % 28):02d}T09:00:00')
            seed_invoice(db, order_id, f'INV-BOX-{index:03d}',
                         created_at=f'2026-10-{1 + (index % 28):02d}T09:00:00')
        get_db().commit()
        view = customer_statement(customer_id)
        source = pdf_source(customer_statement_pdf_bytes(view))

    bottom, top = _summary_box(source)
    assert bottom > STATEMENT_FOOTER_Y, 'totals box must sit above the footer'
    assert top <= 792, 'totals box must stay on the page'
    # One row ⇒ one 14pt line inside an 18pt box (the box maths self-adjusts).
    assert round(top - bottom, 2) == 18.0
