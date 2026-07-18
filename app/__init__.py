import os
from flask import Flask

from .db import init_app as init_db_app
from .routes.auth import bp as auth_bp
from .routes.admin import bp as admin_bp
from .routes.settings import bp as settings_bp
from .routes.public import bp as public_bp
from .routes.inventory import bp as inventory_bp
from .routes.customers import bp as customers_bp
from .routes.orders import bp as orders_bp
from .routes.documents import bp as documents_bp
from .routes.payments import bp as payments_bp
from .routes.branches import bp as branches_bp
from .routes.internal_telegram import bp as internal_telegram_bp


def _truthy_env(name):
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def _validate_production_config(app):
    production = app.config.get("ENV") == "production" or _truthy_env("RENDER") or _truthy_env("PRODUCTION")
    if app.config.get("TESTING") or not production:
        return
    errors = []
    if app.config.get("SECRET_KEY") in {"", "dev-change-me"}:
        errors.append("SECRET_KEY must be set to a non-default value")
    if app.config.get("ADMIN_PASSWORD") in {"", "admin123"}:
        errors.append("ADMIN_PASSWORD must be set to a non-default value")
    if not app.config.get("ADMIN_EMAIL"):
        errors.append("ADMIN_EMAIL must be set")
    if errors:
        raise RuntimeError("Production configuration error: " + "; ".join(errors))


def create_app(test_config=None):
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(app.instance_path, "abi_rental.db")),
        TURSO_DATABASE_URL=os.environ.get("TURSO_DATABASE_URL", ""),
        TURSO_AUTH_TOKEN=os.environ.get("TURSO_AUTH_TOKEN", ""),
        ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL", "admin@abi.local"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin123"),
        ENV=os.environ.get("FLASK_ENV", "development"),
        TELEGRAM_BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        TELEGRAM_CHAT_ID=os.environ.get("TELEGRAM_CHAT_ID", ""),
        TELEGRAM_CRON_SECRET=os.environ.get("TELEGRAM_CRON_SECRET", ""),
        TELEGRAM_NOTIFICATIONS_ENABLED=os.environ.get("TELEGRAM_NOTIFICATIONS_ENABLED", ""),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL", ""),
        BUSINESS_TIMEZONE=os.environ.get("BUSINESS_TIMEZONE", "Africa/Johannesburg"),
        CURRENCY_SYMBOL=os.environ.get("CURRENCY_SYMBOL", "R"),
    )
    if test_config:
        app.config.update(test_config)
    _validate_production_config(app)
    os.makedirs(app.instance_path, exist_ok=True)
    init_db_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(branches_bp)
    app.register_blueprint(internal_telegram_bp)
    app.register_blueprint(public_bp)
    return app
