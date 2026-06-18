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

Milestone 1 and part of Milestone 2 are implemented:
- Flask app factory
- SQLite schema and seed data
- Login/logout
- Admin shell/sidebar/mobile nav
- Setup dashboard
- Dashboard metrics shell
- Settings: general, pricing, taxes, rental period
- Public store empty-state shell
- Placeholder shells for orders, customers, inventory, calendar, documents, online store, reports
- Tests

## Verification

```bash
source .venv/bin/activate
pytest -q
python -m compileall app tests -q
curl -fsS http://127.0.0.1:5057/health
```
