"""ABI-341953266: only statement payment amounts are green, never balances."""
import pymupdf
import pytest

from test_ticket_341953085_customer_statement import (
    app, client, login, seed_ticket_ledger,
)
from test_ticket_341953265_statement_balance import (
    seed_credit_customer, seed_account_payment, seed_payment,
)
from app.db import get_db
from app.services.customers import customer_statement
from app.services.pdf_documents import _statement_draw_row, customer_statement_pdf_bytes


def pdf_spans(data):
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [[span for block in page.get_text("dict")["blocks"]
                 for line in block.get("lines", []) for span in line["spans"]]
                for page in doc]


@pytest.mark.parametrize("kind", ["Payment", "Legacy payment", "Repayment"])
@pytest.mark.parametrize("amount", ["-R300.00", "R50.00"])
def test_green_is_scoped_to_payment_amount_and_resets_immediately(kind, amount):
    commands = []
    _statement_draw_row(commands, {"type": kind, "amount_display": amount,
                                  "balance_display": "R700.00"}, 400)
    start = commands.index("0 0.45 0 rg")
    assert f"({amount})" in commands[start + 1]
    assert commands[start + 2] == "0 0 0 rg"
    assert "(R700.00)" in commands[start + 3]
    assert commands.count("0 0.45 0 rg") == 1


@pytest.mark.parametrize("kind", ["Invoice", "Legacy balance", "Account", "Reversal"])
def test_non_payment_rows_keep_existing_colour(kind):
    commands = []
    _statement_draw_row(commands, {"type": kind, "amount_display": "R300.00"}, 400)
    assert not any(command.endswith(" rg") for command in commands)


@pytest.mark.parametrize("repeat", [1, 35])
def test_mixed_rows_and_continuation_pages_have_only_green_payment_amounts(app, repeat):
    with app.app_context():
        cid = seed_ticket_ledger(get_db())
        get_db().commit()
        view = customer_statement(cid)
        view["activity"] *= repeat
        data = customer_statement_pdf_bytes(view)
    pages = pdf_spans(data)
    assert (len(pages) == 1) if repeat == 1 else (len(pages) > 2)
    green = []
    for spans in pages:
        for span in spans:
            if span["color"] not in (0, 0xFFFFFF):
                green.append(span)
                assert span["text"] in ("-R1150.00", "-R300.00")
                assert span["color"] & 0xFF0000 == 0
                assert span["color"] & 0xFF == 0
                assert 100 <= (span["color"] >> 8) <= 130
    assert len(green) == 2 * repeat


def test_authenticated_customer_statement_route_returns_green_pdf(client, app):
    login(client)
    with app.app_context():
        cid = seed_ticket_ledger(get_db())
        get_db().commit()
    response = client.get(f"/customers/{cid}/statement?date_from=2026-09-01&date_to=2026-09-30")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert "no-store" in response.headers["Cache-Control"]
    green = [span["text"] for spans in pdf_spans(response.data) for span in spans
             if span["color"] not in (0, 0xFFFFFF)]
    assert green == ["-R300.00"]


def test_shared_facility_renderer_colours_repayment_only(app):
    from app.services.account_statements import account_statement
    with app.app_context():
        db = get_db()
        cid, order = seed_credit_customer(db)
        seed_account_payment(db, order, 115.0, "2026-10-07T10:00:00")
        seed_payment(db, order, 30.0, "2026-10-08T10:00:00", method="eft")
        db.commit()
        data = customer_statement_pdf_bytes(account_statement(cid))
    green = [span["text"] for spans in pdf_spans(data) for span in spans
             if span["color"] not in (0, 0xFFFFFF)]
    assert green == ["-R30.00"]
