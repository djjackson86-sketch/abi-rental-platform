from app.db import get_db, now

def init_app_store(db):
    db.execute("""CREATE TABLE IF NOT EXISTS app_store_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 0,
        config_json TEXT NOT NULL DEFAULT '{}',
        installed_at TEXT,
        updated_at TEXT NOT NULL
    )""")
    db.commit()

def list_app_store_items():
    db = get_db()
    init_app_store(db)  # ensure table exists
    return db.execute("SELECT * FROM app_store_items ORDER BY name").fetchall()

def update_app_store_item(item_id, is_active=None, config_json=None):
    db = get_db()
    init_app_store(db)
    updates = []
    params = []
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if is_active else 0)
    if config_json is not None:
        # Validate JSON
        import json
        json.loads(config_json)  # will raise if invalid
        updates.append("config_json = ?")
        params.append(config_json)
    if not updates:
        return
    updates.append("updated_at = ?")
    params.append(now())
    params.append(item_id)
    db.execute(f"UPDATE app_store_items SET {', '.join(updates)} WHERE id = ?", params)
    db.commit()

def seed_app_store_items():
    db = get_db()
    init_app_store(db)
    # Check if already seeded
    existing = db.execute("SELECT COUNT(*) FROM app_store_items").fetchone()[0]
    if existing > 0:
        return
    items = [
        ("ShipStation", "Manage shipping and fulfillment via ShipStation", "shipstation", 0, '{"api_key": "", "api_secret": ""}'),
        ("Mailchimp", "Sync customers and send email campaigns via Mailchimp", "mailchimp", 0, '{"api_key": "", "audience_id": ""}'),
        ("Stripe Connect", "Accept credit card payments via Stripe Connect", "stripe", 0, '{"publishable_key": "", "secret_key": ""}'),
        ("PayPal Commerce", "Accept PayPal and Venmo payments", "paypal", 0, '{"client_id": "", "secret": ""}'),
        ("QuickBooks Sync", "Synchronize invoices, payments, and customers with QuickBooks Online", "quickbooks", 0, '{"client_id": "", "client_secret": "", "refresh_token": ""}'),
        ("Xero Accounting", "Synchronize invoices, payments, and contacts with Xero", "xero", 0, '{"client_id": "", "client_secret": ""}'),
        ("Google Calendar Sync", "Sync bookings and appointments with Google Calendar", "google_calendar", 0, '{"calendar_id": "", "api_key": ""}'),
        ("Twilio SMS", "Send SMS notifications via Twilio", "twilio", 0, '{"account_sid": "", "auth_token": "", "from_number": ""}'),
        ("Slack Alerts", "Send order and low-stock alerts to Slack channels", "slack", 0, '{"webhook_url": "", "channel": ""}'),
        ("Dropbox Backup", "Automatically backup database and attachments to Dropbox", "dropbox", 0, '{"access_token": ""}'),
    ]
    for name, desc, provider, active, config in items:
        db.execute(
            """INSERT INTO app_store_items (name, description, provider, is_active, config_json, installed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (name, desc, provider, 1 if active else 0, config, now(), now())
        )
    db.commit()