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
        ADMIN_EMAIL=os.environ.get("ADMIN_EMAIL", "admin@abi.local"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD", "admin123"),
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    init_db_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(public_bp)
    return app
