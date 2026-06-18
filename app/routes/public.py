from flask import Blueprint, render_template
from app.db import get_db
from app.services.settings import get_company_settings

bp = Blueprint("public", __name__)


@bp.route("/store")
def store():
    products = get_db().execute("SELECT * FROM products WHERE active = 1 AND public_visible = 1 ORDER BY name").fetchall()
    return render_template("public/store.html", settings=get_company_settings(), products=products)
