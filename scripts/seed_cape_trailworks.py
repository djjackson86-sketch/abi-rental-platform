"""Seed the Cape Trailworks Booqable-parity fixture into the local ABI database.

Idempotent: updates existing fixture records by SKU/email and creates the three
workflow orders once, marked with a Cape Trailworks parity note.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db, now
from app.services.orders import create_order, transition_order

COMPANY = {
    "company_name": "Cape Trailworks Rentals & Service",
    "email": "bookings@capetrailworks.test",
    "phone": "+27 21 555 0188",
    "website": "https://capetrailworks.test",
    "country": "South Africa",
    "address_line1": "18 Marine Drive",
    "address_line2": "Paarden Eiland",
    "city": "Cape Town",
    "province": "Western Cape",
    "postcode": "7405",
    "timezone": "Africa/Johannesburg",
    "first_day_of_week": "Sunday",
    "date_format": "dd-mm-yyyy",
    "units": "metric",
    "currency": "ZAR",
    "currency_symbol": "R",
    "currency_position": "before",
    "tax_mode": "exclusive",
    "default_pickup_time": "08:00",
    "default_return_time": "16:00",
    "time_increment_minutes": 60,
    "deposit_mode": "product_specific",
    "deposit_value": 0,
    "store_title": "Cape Trailworks",
    "store_intro": "Trailer hire, trailer parts, and workshop service options for Cape Town businesses and weekend movers.",
    "store_hero_text": "Book trailers, buy parts, or add workshop services in one request.",
    "checkout_instructions": "Submit your booking request and our team will confirm availability before payment.",
    "store_contact_email": "bookings@capetrailworks.test",
    "store_contact_phone": "+27 21 555 0188",
}

PRODUCTS = [
    ("750kg Utility Trailer", "CTW-TRL-750U", "rental", 4, 250, "day", 1000, "General-purpose open trailer, unbraked."),
    ("1.5 Ton Braked Trailer", "CTW-TRL-1500B", "rental", 3, 450, "day", 1500, "Braked axle, furniture and equipment moves."),
    ("Enclosed Furniture Trailer", "CTW-TRL-ENC", "rental", 2, 650, "day", 2500, "Weather-protected box trailer."),
    ("Car Transporter Trailer", "CTW-TRL-CAR", "rental", 1, 950, "day", 4000, "Vehicle transporter with ramps and winch."),
    ("Bike Trailer", "CTW-TRL-BIKE", "rental", 2, 300, "day", 1200, "Two-bike motorbike trailer."),
    ("LED Trailer Light Kit", "CTW-PART-LIGHTKIT", "sale", 20, 475, "fixed", 0, "Left/right LED light kit with wiring."),
    ("48mm Jockey Wheel", "CTW-PART-JOCKEY48", "sale", 15, 695, "fixed", 0, "Clamp-on jockey wheel."),
    ("Coupler Lock", "CTW-PART-LOCK", "sale", 25, 285, "fixed", 0, "Anti-theft hitch/coupler lock."),
    ("7 Pin Trailer Plug", "CTW-PART-7PIN", "sale", 30, 95, "fixed", 0, "Replacement 7-pin plug."),
    ("Wheel Bearing Kit", "CTW-PART-BEARING", "sale", 18, 350, "fixed", 0, "Standard trailer wheel bearing kit."),
    ("Trailer Safety Inspection", "CTW-SVC-SAFE", "service", 999, 550, "fixed", 0, "Lights, tyres, coupling, brakes, chassis check."),
    ("Bearing Replacement Labour", "CTW-SVC-BEARING", "service", 999, 750, "fixed", 0, "Labour only; parts billed separately."),
    ("Light Wiring Repair", "CTW-SVC-WIRING", "service", 999, 650, "fixed", 0, "Diagnose and repair trailer wiring faults."),
    ("Brake Service", "CTW-SVC-BRAKES", "service", 999, 1250, "fixed", 0, "Brake adjustment/service on braked trailers."),
    ("Coupler Replacement Labour", "CTW-SVC-COUPLER", "service", 999, 600, "fixed", 0, "Labour only; parts billed separately."),
]

CUSTOMERS = [
    ("individual", "Nomvula Dlamini", "nomvula@sample.test", "+27 82 555 0101", 1),
    ("company", "Metro Events Logistics", "ops@metroevents.test", "+27 21 555 0199", 0),
]


def upsert_product(db, tax_id, product):
    name, sku, product_type, quantity, price, unit, deposit, description = product
    existing = db.execute("SELECT id FROM products WHERE sku = ?", (sku,)).fetchone()
    values = (name, product_type, "bulk", description, sku, 1, 1, price, unit, deposit, tax_id, quantity)
    if existing:
        db.execute(
            """UPDATE products SET name=?, product_type=?, tracking_method=?, description=?, sku=?, active=?, public_visible=?,
            price_amount=?, price_unit=?, security_deposit=?, tax_profile_id=?, quantity=? WHERE id=?""",
            (*values, existing["id"]),
        )
        return existing["id"]
    cur = db.execute(
        """INSERT INTO products (name, product_type, tracking_method, description, sku, active, public_visible, price_amount, price_unit, security_deposit, tax_profile_id, quantity, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (*values, now()),
    )
    return cur.lastrowid


def upsert_customer(db, customer):
    customer_type, name, email, phone, marketing = customer
    existing = db.execute("SELECT id FROM customers WHERE email = ?", (email,)).fetchone()
    if existing:
        db.execute(
            "UPDATE customers SET customer_type=?, name=?, phone=?, marketing_opt_in=? WHERE id=?",
            (customer_type, name, phone, marketing, existing["id"]),
        )
        return existing["id"]
    cur = db.execute(
        "INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, balance_due, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
        (customer_type, name, email, phone, marketing, now()),
    )
    return cur.lastrowid


def configure_company_and_tax(db):
    set_clause = ", ".join([f"{key}=?" for key in COMPANY])
    db.execute(f"UPDATE company_settings SET {set_clause}, updated_at=? WHERE id=1", [*COMPANY.values(), now()])
    db.execute("UPDATE tax_profiles SET is_default = 0")
    tax = db.execute("SELECT id FROM tax_profiles WHERE name='VAT 15%' AND active=1").fetchone()
    if tax:
        tax_id = tax["id"]
        db.execute("UPDATE tax_profiles SET rate=15, is_default=1 WHERE id=?", (tax_id,))
    else:
        cur = db.execute(
            "INSERT INTO tax_profiles (name, rate, is_default, active, created_at) VALUES ('VAT 15%', 15, 1, 1, ?)",
            (now(),),
        )
        tax_id = cur.lastrowid
    return tax_id


def create_workflow_orders(db, product_ids, customer_ids):
    existing = db.execute("SELECT COUNT(*) AS count FROM orders WHERE notes LIKE 'Cape Trailworks parity:%'").fetchone()["count"]
    if existing:
        return 0

    order_1 = create_order(MultiDict([
        ("customer_id", str(customer_ids["nomvula@sample.test"])),
        ("product_id", str(product_ids["CTW-TRL-750U"])), ("quantity", "1"),
        ("start_date", "2026-07-06"), ("start_time", "08:00"),
        ("end_date", "2026-07-08"), ("end_time", "16:00"),
        ("notes", "Cape Trailworks parity: rental-only order"),
    ]))
    transition_order(order_1, "reserve")
    transition_order(order_1, "start")
    transition_order(order_1, "return")

    order_2 = create_order(MultiDict([
        ("customer_id", str(customer_ids["ops@metroevents.test"])),
        ("product_id", str(product_ids["CTW-TRL-ENC"])), ("quantity", "1"),
        ("product_id", str(product_ids["CTW-PART-LIGHTKIT"])), ("quantity", "2"),
        ("product_id", str(product_ids["CTW-SVC-SAFE"])), ("quantity", "1"),
        ("start_date", "2026-07-10"), ("start_time", "08:00"),
        ("end_date", "2026-07-12"), ("end_time", "16:00"),
        ("notes", "Cape Trailworks parity: mixed rental sale service order"),
    ]))
    transition_order(order_2, "reserve")

    create_order(MultiDict([
        ("customer_id", str(customer_ids["ops@metroevents.test"])),
        ("product_id", str(product_ids["CTW-TRL-CAR"])), ("quantity", "2"),
        ("start_date", "2026-07-10"), ("start_time", "08:00"),
        ("end_date", "2026-07-12"), ("end_time", "16:00"),
        ("notes", "Cape Trailworks parity: shortage draft order"),
    ]))
    return 3


def main():
    app = create_app()
    with app.app_context():
        db = get_db()
        tax_id = configure_company_and_tax(db)
        product_ids = {product[1]: upsert_product(db, tax_id, product) for product in PRODUCTS}
        customer_ids = {customer[2]: upsert_customer(db, customer) for customer in CUSTOMERS}
        db.commit()
        created_orders = create_workflow_orders(db, product_ids, customer_ids)
        print(f"Seeded Cape Trailworks fixture: {len(PRODUCTS)} products, {len(CUSTOMERS)} customers, {created_orders} new orders")


if __name__ == "__main__":
    main()
