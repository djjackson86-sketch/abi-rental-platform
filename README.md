# ABI Rental Platform

Booqable-inspired ABI-branded rental management platform.

## Local setup

```bash
cd /mnt/d/Hermes/abi-rental-platform
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
python app.py
```

Open: http://127.0.0.1:5057

Seed login:
- Email: admin@abi.local
- Password: admin123

## Current milestone

Milestone 1, part of Milestone 2, and the Inventory slice are implemented:
- Flask app factory
- SQLite schema and seed data
- Login/logout
- Admin shell/sidebar/mobile nav
- Setup dashboard
- Dashboard metrics shell
- Settings: general, pricing, taxes, rental period
- DB-backed inventory/product list
- Add/edit/archive product flow
- Product type, price, stock, deposit, active/public visibility fields
- DB-backed customer list/detail
- Add/edit customer flow
- Individual/company type, email, phone and marketing opt-in fields
- DB-backed orders list/detail
- New draft order form with customer/product pickers
- Server-side draft order calculations for rental days, subtotal, tax, deposit, total and due
- Public store product cards from DB
- Placeholder shells for calendar, documents, online store, reports
- Tests

## Verification

```bash
source .venv/bin/activate
pytest -q
python -m compileall app tests -q
curl -fsS http://127.0.0.1:5057/health
```
