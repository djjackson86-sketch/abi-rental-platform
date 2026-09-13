from flask import Blueprint, flash, redirect, render_template, request, url_for
from app.routes.auth import login_required
from app.services.branches import branch_hours_summaries, create_branch, delete_branch, get_branch, list_branch_hours, list_branches, save_branch_hours, update_branch
from app.services.settings import get_company_settings

bp = Blueprint("branches", __name__, url_prefix="/branches")

@bp.post("/<int:branch_id>/delete")
@login_required
def delete(branch_id):
    try:
        delete_branch(branch_id)
        flash("Branch deleted", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("branches.index"))


@bp.post("/<int:branch_id>/hours")
@login_required
def hours(branch_id):
    """Save the branch's trading hours (informational - never enforced)."""
    try:
        save_branch_hours(branch_id, request.form)
        flash("Trading hours saved", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("branches.index", edit=branch_id))


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
    return render_template(
        "admin/branches/index.html",
        settings=get_company_settings(),
        branches=list_branches(),
        edit_branch=get_branch(edit_id) if edit_id else None,
        branch_hours=list_branch_hours(edit_id) if edit_id else [],
        hours_summaries=branch_hours_summaries(),
    )
