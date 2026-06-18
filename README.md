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

Milestone 1, part of Milestone 2, Inventory, Customers, Draft Orders, Reservations foundation, Documents foundation, and Public Checkout foundation are implemented:
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
- Order status workflow: reserve, start, return, archive, cancel
- Availability check that prevents overbooking reserved/started rental stock
- Reservation calendar with active reserved/started orders
- Dashboard going-out and coming-back schedule panels
- Documents list and printable document detail pages
- Order-level document generation for quotes, contracts, invoices and packing slips
- Sequential document numbering by type
- Public store product cards from DB
- Public product detail pages with booking request form
- Public checkout customer capture and draft order creation
- Public booking confirmation page with order estimate
- Placeholder shells for online store settings and reports
- Tests

## Verification

```bash
source .venv/bin/activate
pytest -q
python -m compileall app tests -q
curl -fsS http://127.0.0.1:5057/health
```
