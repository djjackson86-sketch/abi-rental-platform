from flask import Blueprint, flash, redirect, render_template, request, url_for
from app.routes.auth import login_required
from app.services.branches import create_branch, get_branch, list_branches, update_branch
from app.services.settings import get_company_settings

bp = Blueprint("branches", __name__, url_prefix="/branches")

@bp.route("", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        try:
            branch_id = request.form.get("branch_id")
            if branch_id:
                update_branch(int(branch_id), request.form)
                flash("Branch saved", "success")
            else:
                create_branch(request.form)
                flash("Branch created", "success")
            return redirect(url_for("branches.index"))
        except Exception as exc:
            flash(str(exc), "error")
    edit_id = request.args.get("edit", type=int)
    return render_template("admin/branches/index.html", settings=get_company_settings(), branches=list_branches(), edit_branch=get_branch(edit_id) if edit_id else None)
