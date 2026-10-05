"""Seed demo stock, customers, vehicles and orders into the isolated side instance.

Safe to run more than once: products, customers and vehicles are matched on their
natural key (SKU / email / plate) and updated, and the demo orders are created only
when none carrying the demo note exist yet.

Usage (inside the app's own environment, against whatever DATABASE_PATH points at)::

    python scripts/seed_side_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from werkzeug.datastructures import MultiDict

from app import create_app
from app.db import get_db, now
from app.services.documents import create_document, finalize_document
from app.services.orders import create_order, get_order, transition_order, update_return_checklist
from app.services.payments import record_payment
from scripts.seed_default_group_images import seed_group_images

DEMO_NOTE = "SIDE-DEMO"

DEMO_GROUPS = [
    ("Single Axle Trailers", "Compact everyday rental trailers for furniture moves, garden refuse and light cargo.", 10),
    ("Single Axle Tarp Trailers", "Covered single-axle trailers for loads that need light weather protection.", 20),
    ("Single Axle Flatbed Trailers", "Open flatbed single-axle trailers for pallets, boards and odd-shaped loads.", 30),
    ("Single Axle Car Trailers", "Specialist vehicle and bike transport trailers with ramps and tie-down points.", 40),
    ("Quad Bike Golf Cart Trailers", "Low-bed trailers for quad bikes, golf carts and compact equipment.", 50),
    ("Piggyback Dolly Trailers", "Dolly and piggyback trailers for moving trailer combinations and yard equipment.", 60),
    ("Luggage Trailers", "Lockable luggage trailers for holidays, events and overflow transport.", 70),
    ("Livestock Trailers", "Ventilated trailers for small livestock and agricultural transport.", 80),
    ("Double Axle Trailers", "Braked double-axle trailers for heavier equipment and longer-distance hauling.", 90),
    ("Double Axle Flatbed Trailers", "Heavy-duty flatbed trailers for plant, pallets and construction materials.", 100),
    ("Double Axle Car Trailers", "Twin-axle car carriers for safer vehicle recovery and longer hauls.", 110),
    ("Closed Box Trailers", "Weather-protected enclosed trailers for furniture, events and boxed goods.", 120),
]

PRODUCT_GROUP_BY_SKU = {
    "DEMO-TRL-750U": "Single Axle Trailers",
    "DEMO-TRL-TARP": "Single Axle Tarp Trailers",
    "DEMO-TRL-SFLAT": "Single Axle Flatbed Trailers",
    "DEMO-TRL-CAR": "Single Axle Car Trailers",
    "DEMO-TRL-QUAD": "Quad Bike Golf Cart Trailers",
    "DEMO-TRL-DOLLY": "Piggyback Dolly Trailers",
    "DEMO-TRL-LUG": "Luggage Trailers",
    "DEMO-TRL-LIVE": "Livestock Trailers",
    "DEMO-TRL-1500B": "Double Axle Trailers",
    "DEMO-TRL-DFLAT": "Double Axle Flatbed Trailers",
    "DEMO-TRL-DCAR": "Double Axle Car Trailers",
    "DEMO-TRL-ENC": "Closed Box Trailers",
}

#: name, sku, product_type, quantity, price, price_unit, deposit, plate, licence no, NaTIS no, description
PRODUCTS = [
    ("750kg Utility Trailer", "DEMO-TRL-750U", "rental", 4, 250, "day", 1000, "NBB 1234", "5120367QP4HD", "QWR419V", "Unbraked general-purpose open trailer."),
    ("Single Axle Tarp Trailer", "DEMO-TRL-TARP", "rental", 3, 320, "day", 1200, "NBG 1122", "5120372QP9LM", "QWR424V", "Covered single-axle trailer for luggage and boxed goods."),
    ("Single Axle Flatbed Trailer", "DEMO-TRL-SFLAT", "rental", 2, 380, "day", 1500, "NBH 3344", "5120373QP1NP", "QWR425V", "Flatbed trailer for pallets and long materials."),
    ("Single Axle Car Trailer", "DEMO-TRL-CAR", "rental", 1, 850, "day", 3500, "NBE 3456", "5120370QP7JG", "QWR422V", "Single-axle vehicle transporter with ramps."),
    ("Quad Bike / Golf Cart Trailer", "DEMO-TRL-QUAD", "rental", 2, 420, "day", 1800, "NBJ 5566", "5120374QP2RQ", "QWR426V", "Low-bed trailer for quad bikes and golf carts."),
    ("Piggyback Dolly Trailer", "DEMO-TRL-DOLLY", "rental", 1, 500, "day", 2000, "NBK 7788", "5120375QP3ST", "QWR427V", "Dolly trailer for yard and trailer-combination moves."),
    ("Luggage Trailer", "DEMO-TRL-LUG", "rental", 4, 280, "day", 1000, "NBL 9900", "5120376QP4UV", "QWR428V", "Lockable holiday and event luggage trailer."),
    ("Livestock Trailer", "DEMO-TRL-LIVE", "rental", 1, 700, "day", 3000, "NBM 2468", "5120377QP5WX", "QWR429V", "Ventilated trailer for small livestock transport."),
    ("1.5 Ton Braked Trailer", "DEMO-TRL-1500B", "rental", 3, 450, "day", 1500, "NBC 5678", "5120368QP5HE", "QWR420V", "Braked axle for furniture and equipment."),
    ("Double Axle Flatbed Trailer", "DEMO-TRL-DFLAT", "rental", 2, 680, "day", 2800, "NBN 1357", "5120378QP6YZ", "QWR430V", "Heavy-duty flatbed for plant and construction materials."),
    ("Double Axle Car Trailer", "DEMO-TRL-DCAR", "rental", 1, 1100, "day", 4500, "NBP 9753", "5120379QP7AB", "QWR431V", "Twin-axle car carrier for longer hauls."),
    ("Enclosed Box Trailer", "DEMO-TRL-ENC", "rental", 2, 650, "day", 2500, "NBD 9012", "5120369QP6IF", "QWR421V", "Weather-protected enclosed trailer."),
    ("LED Trailer Light Kit", "DEMO-PART-LIGHT", "sale", 20, 475, "fixed", 0, "", "", "", "Left/right LED light kit with wiring."),
    ("48mm Jockey Wheel", "DEMO-PART-JOCKEY", "sale", 15, 695, "fixed", 0, "", "", "", "Clamp-on jockey wheel."),
    ("Wheel Bearing Kit", "DEMO-PART-BEAR", "sale", 18, 350, "fixed", 0, "", "", "", "Standard trailer wheel bearing kit."),
    ("Trailer Safety Inspection", "DEMO-SVC-SAFE", "service", 999, 550, "fixed", 0, "", "", "", "Lights, tyres, coupling, brakes, chassis."),
    ("Brake Service", "DEMO-SVC-BRAKE", "service", 999, 1250, "fixed", 0, "", "", "", "Brake adjustment/service on braked trailers."),
]

#: customer_type, name, email, phone, marketing_opt_in, address
CUSTOMERS = [
    ("individual", "Thabo Mokoena", "thabo.mokoena@demo.test", "082 555 0101", 1, "12 Acacia Road", "Midrand", "Gauteng", "1685"),
    ("individual", "Charmaine Naidoo", "charmaine.naidoo@demo.test", "083 555 0102", 0, "4 Protea Close", "Wonderboom", "Gauteng", "0182"),
    ("company", "Highveld Site Services", "ops@highveldsite.test", "011 555 0103", 0, "Unit 7, Kya Sand Industrial", "Randburg", "Gauteng", "2163"),
    ("company", "Roodepoort Garden Co", "admin@roodepoortgarden.test", "010 555 0104", 1, "88 Main Reef Road", "Roodepoort", "Gauteng", "1724"),
    ("individual", "Pieter van Wyk", "pieter.vanwyk@demo.test", "084 555 0105", 0, "23 Voortrekker Street", "Vereeniging", "Gauteng", "1930"),
]

#: plate, NaTIS no, licence no, make, VIN, engine, expiry, customer email
VEHICLES = [
    ("NB 72 XMGP", "QWR419V", "5120367QP4HD", "TOYOTA", "AHTFR22G50L123456", "2GD1234567", "2027-03-31", "thabo.mokoena@demo.test"),
    ("CA 445 112", "CAW778L", "4880123QP2MN", "ISUZU", "MPATFS85JJT009871", "4JJ1T654321", "2026-11-30", "charmaine.naidoo@demo.test"),
    ("JT 889 001", "JTW118P", "4770456QP9RT", "NISSAN", "JN1TBNT30Z0001234", "YD25-889001", "2027-01-31", "ops@highveldsite.test"),
]


def upsert_product(db, tax_id, product):
    (name, sku, product_type, quantity, price, unit, deposit, plate, licence_no, natis_no, description) = product
    existing = db.execute("SELECT id FROM products WHERE sku = ?", (sku,)).fetchone()
    values = (
        name, product_type, "bulk", description, sku, 1, 1, price, unit, deposit,
        plate, licence_no, natis_no, tax_id, quantity,
    )
    if existing:
        db.execute(
            """UPDATE products SET name=?, product_type=?, tracking_method=?, description=?, sku=?,
               active=?, public_visible=?, price_amount=?, price_unit=?, security_deposit=?,
               registration=?, licence_number=?, registration_number=?, tax_profile_id=?, quantity=?
               WHERE id=?""",
            (*values, existing["id"]),
        )
        return existing["id"]
    cur = db.execute(
        """INSERT INTO products (name, product_type, tracking_method, description, sku, active,
           public_visible, price_amount, price_unit, security_deposit, registration, licence_number,
           registration_number, tax_profile_id, quantity, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (*values, now()),
    )
    return cur.lastrowid


def upsert_group(db, group):
    name, description, sort_order = group
    existing = db.execute("SELECT id FROM product_groups WHERE name = ?", (name,)).fetchone()
    if existing:
        db.execute(
            """UPDATE product_groups SET description=?, active=1, becomes_store_visible=1,
               sort_order=?, updated_at=? WHERE id=?""",
            (description, sort_order, now(), existing["id"]),
        )
        return existing["id"]
    cur = db.execute(
        """INSERT INTO product_groups (name, description, active, becomes_store_visible, sort_order, created_at, updated_at)
           VALUES (?, ?, 1, 1, ?, ?, ?)""",
        (name, description, sort_order, now(), now()),
    )
    return cur.lastrowid


def assign_demo_products_to_groups(db, product_ids, group_ids):
    for sku, group_name in PRODUCT_GROUP_BY_SKU.items():
        product_id = product_ids.get(sku)
        group_id = group_ids.get(group_name)
        if product_id and group_id:
            db.execute("UPDATE products SET product_group_id = ? WHERE id = ?", (group_id, product_id))


def upsert_customer(db, branch_id, customer):
    (customer_type, name, email, phone, marketing, address_line1, city, province, postal_code) = customer
    existing = db.execute("SELECT id FROM customers WHERE email = ?", (email,)).fetchone()
    if existing:
        db.execute(
            """UPDATE customers SET customer_type=?, name=?, phone=?, marketing_opt_in=?, address_line1=?,
               city=?, province=?, postal_code=?, branch_id=? WHERE id=?""",
            (customer_type, name, phone, marketing, address_line1, city, province, postal_code, branch_id, existing["id"]),
        )
        return existing["id"]
    cur = db.execute(
        """INSERT INTO customers (customer_type, name, email, phone, marketing_opt_in, address_line1,
           city, province, postal_code, balance_due, branch_id, source_system, source_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'demo', ?, ?)""",
        (customer_type, name, email, phone, marketing, address_line1, city, province, postal_code,
         branch_id, f"side-demo-{email}", now()),
    )
    return cur.lastrowid


def upsert_vehicle(db, customer_id, vehicle):
    (plate, natis_no, licence_no, make, vin, engine, expiry, _email) = vehicle
    existing = db.execute("SELECT id FROM vehicles WHERE registration = ?", (plate,)).fetchone()
    values = (plate, natis_no, licence_no, make, vin, engine, expiry, customer_id)
    if existing:
        db.execute(
            """UPDATE vehicles SET registration=?, registration_number=?, licence_number=?, make=?,
               vin=?, engine_number=?, licence_disk_expiry=?, customer_id=?, updated_at=? WHERE id=?""",
            (*values, now(), existing["id"]),
        )
        return existing["id"]
    cur = db.execute(
        """INSERT INTO vehicles (registration, registration_number, licence_number, make, vin,
           engine_number, licence_disk_expiry, customer_id, source, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?)""",
        (*values, now(), now()),
    )
    return cur.lastrowid


def demo_orders_exist(db):
    row = db.execute("SELECT COUNT(*) AS count FROM orders WHERE notes LIKE ?", (f"{DEMO_NOTE}%",)).fetchone()
    return bool(row["count"])


def pay_full(order_id, method="eft", reference="DEMO-PAY"):
    order = get_order(order_id)
    amount = float(order["due_total"] or order["total"] or 0)
    if amount <= 0:
        return None
    record_payment(order_id, MultiDict([
        ("amount", f"{amount:.2f}"), ("method", method), ("reference", reference),
    ]))
    return amount


def create_demo_orders(product_ids, customer_ids):
    orders = {}

    draft = create_order(MultiDict([
        ("customer_id", str(customer_ids["thabo.mokoena@demo.test"])),
        ("product_id", str(product_ids["DEMO-TRL-750U"])), ("quantity", "1"),
        ("start_date", "2026-10-06"), ("start_time", "08:00"),
        ("end_date", "2026-10-08"), ("end_time", "16:00"),
        ("notes", f"{DEMO_NOTE}: draft rental"),
    ]))
    orders["draft"] = draft

    reserved = create_order(MultiDict([
        ("customer_id", str(customer_ids["charmaine.naidoo@demo.test"])),
        ("product_id", str(product_ids["DEMO-TRL-1500B"])), ("quantity", "1"),
        ("start_date", "2026-10-09"), ("start_time", "07:30"),
        ("end_date", "2026-10-12"), ("end_time", "16:00"),
        ("notes", f"{DEMO_NOTE}: reserved rental"),
    ]))
    transition_order(reserved, "reserve")
    orders["reserved"] = reserved

    started = create_order(MultiDict([
        ("customer_id", str(customer_ids["ops@highveldsite.test"])),
        ("product_id", str(product_ids["DEMO-TRL-ENC"])), ("quantity", "1"),
        ("product_id", str(product_ids["DEMO-SVC-SAFE"])), ("quantity", "1"),
        ("start_date", "2026-10-01"), ("start_time", "08:00"),
        ("end_date", "2026-10-05"), ("end_time", "16:00"),
        ("notes", f"{DEMO_NOTE}: started rental with service line"),
    ]))
    transition_order(started, "reserve")
    transition_order(started, "start")
    pay_full(started, method="card", reference="DEMO-CARD-1")
    orders["started"] = started

    closed = create_order(MultiDict([
        ("customer_id", str(customer_ids["admin@roodepoortgarden.test"])),
        ("product_id", str(product_ids["DEMO-TRL-QUAD"])), ("quantity", "1"),
        ("start_date", "2026-09-15"), ("start_time", "08:00"),
        ("end_date", "2026-09-18"), ("end_time", "16:00"),
        ("notes", f"{DEMO_NOTE}: completed rental, paid and invoiced"),
    ]))
    transition_order(closed, "reserve")
    transition_order(closed, "start")
    pay_full(closed, method="eft", reference="DEMO-EFT-2")
    try:
        document_id = create_document(closed, "invoice")
        finalize_document(document_id)
    except ValueError:
        pass
    # The return gate wants the checklist answered: no damages, no date revision.
    update_return_checklist(closed, MultiDict([("no_damages", "1"), ("no_revision_required", "1")]))
    transition_order(closed, "return")
    orders["closed"] = closed

    sale = create_order(MultiDict([
        ("customer_id", str(customer_ids["pieter.vanwyk@demo.test"])),
        ("product_id", str(product_ids["DEMO-PART-LIGHT"])), ("quantity", "2"),
        ("product_id", str(product_ids["DEMO-PART-JOCKEY"])), ("quantity", "1"),
        ("start_date", "2026-10-01"), ("start_time", "08:00"),
        ("end_date", "2026-10-01"), ("end_time", "16:00"),
        ("notes", f"{DEMO_NOTE}: parts sale, paid"),
    ]))
    transition_order(sale, "reserve")
    pay_full(sale, method="cash", reference="DEMO-CASH-3")
    orders["sale"] = sale
    return orders


def seed(app=None):
    """Load the side-demo dataset. Idempotent: safe to run repeatedly.

    ``app`` is supplied when the seed runs inside a running process (the temporary
    token-guarded internal route on the side service, whose database lives on a
    Render disk that cannot be written from a developer machine). Called with no
    app it builds one from the environment, which is the CLI path used locally.
    """
    app = app or create_app()
    with app.app_context():
        db = get_db()
        tax = db.execute("SELECT id FROM tax_profiles WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
        if tax:
            tax_id = tax["id"]
        else:
            tax_id = db.execute(
                "INSERT INTO tax_profiles (name, rate, is_default, active, created_at) VALUES ('VAT 15%', 15, 1, 1, ?)",
                (now(),),
            ).lastrowid
        branch = db.execute("SELECT id FROM branches WHERE active = 1 ORDER BY id LIMIT 1").fetchone()
        branch_id = branch["id"] if branch else None

        group_ids = {group[0]: upsert_group(db, group) for group in DEMO_GROUPS}
        product_ids = {product[1]: upsert_product(db, tax_id, product) for product in PRODUCTS}
        assign_demo_products_to_groups(db, product_ids, group_ids)
        customer_ids = {customer[2]: upsert_customer(db, branch_id, customer) for customer in CUSTOMERS}
        vehicle_ids = []
        for vehicle in VEHICLES:
            vehicle_ids.append(upsert_vehicle(db, customer_ids[vehicle[7]], vehicle))
        db.commit()

        created = {}
        if not demo_orders_exist(db):
            created = create_demo_orders(product_ids, customer_ids)
        image_results = seed_group_images(ROOT / "static" / "img" / "trailer-categories-web", commit=True)

        return {
            "groups": len(group_ids),
            "products": len(product_ids),
            "customers": len(customer_ids),
            "vehicles": len(vehicle_ids),
            "orders_created": len(created),
            "category_images": sum(1 for row in image_results if row["outcome"] in {"set", "already", "protected"}),
            "branches": branch_id,
        }


if __name__ == "__main__":
    print(seed())
