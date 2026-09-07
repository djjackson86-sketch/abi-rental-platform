from flask import Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, url_for
import csv
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
import secrets
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

from app.db import get_db, now

from app.routes.auth import login_required
from app.services.documents import create_document, display_document_label, display_document_number, document_date, document_datetime, document_filter_counts, finalize_document, get_document, label_for, list_documents, mark_document_email, printable_document, rental_days_label
from app.services.settings import get_company_settings
from app.services.email_delivery import build_invoice_email_subject, build_outlook_draft_eml, render_email_template
from app.services.pdf_documents import DOCUMENT_LOGO_STATIC_PATH, document_pdf_bytes, document_pdf_filename
from app.services.customers import custom_fields_for

bp = Blueprint("documents", __name__, url_prefix="/documents")

EMAIL_DRAFT_DOCUMENT_TYPES = {'invoice', 'quote'}
EMAIL_HELPER_TOKEN_HOURS = 24


def _email_helper_scripts(base_url):
    ps1 = rf'''param(
    [Parameter(Mandatory=$true)]
    [string]$ProtocolUrl
)

Add-Type -AssemblyName System.Web
$ErrorActionPreference = "Stop"

try {{
    $uri = [Uri]$ProtocolUrl
    if ($uri.Scheme -ne "abi-email") {{ throw "Invalid protocol: $($uri.Scheme)" }}
    $query = [System.Web.HttpUtility]::ParseQueryString($uri.Query)
    $downloadUrl = $query.Get("url")
    $fileName = $query.Get("filename")
    if ([string]::IsNullOrWhiteSpace($downloadUrl)) {{ throw "Missing email draft URL" }}
    if ([string]::IsNullOrWhiteSpace($fileName)) {{ $fileName = "ABI-email-draft.eml" }}

    $downloadUri = [Uri]$downloadUrl
    $allowedHosts = @("abi-rental-platform.onrender.com", "localhost", "127.0.0.1")
    if ($allowedHosts -notcontains $downloadUri.Host.ToLowerInvariant()) {{
        throw "Blocked email draft host: $($downloadUri.Host)"
    }}
    if ($downloadUri.Scheme -notin @("https", "http")) {{ throw "Blocked URL scheme: $($downloadUri.Scheme)" }}

    $safeName = ($fileName -replace '[^A-Za-z0-9_. -]', '_')
    if (-not $safeName.ToLowerInvariant().EndsWith(".eml")) {{ $safeName = $safeName + ".eml" }}
    $targetDir = Join-Path $env:TEMP "ABIEmailDrafts"
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    $targetPath = Join-Path $targetDir $safeName

    Invoke-WebRequest -Uri $downloadUri.AbsoluteUri -OutFile $targetPath -UseBasicParsing
    if (-not (Test-Path $targetPath)) {{ throw "Email draft did not download" }}
    Start-Process -FilePath $targetPath
}} catch {{
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("ABI Email Helper could not open the draft.`n`n$($_.Exception.Message)`n`nUse Generate Email in ABI Rental as a fallback.", "ABI Email Helper") | Out-Null
    exit 1
}}
'''
    install_cmd = r'''@echo off
setlocal
set "HELPER_DIR=%LOCALAPPDATA%\ABIEmailHelper"
mkdir "%HELPER_DIR%" >nul 2>&1
copy /Y "%~dp0abi-email-helper.ps1" "%HELPER_DIR%\abi-email-helper.ps1" >nul
reg add "HKCU\Software\Classes\abi-email" /ve /d "URL:ABI Email Helper" /f >nul
reg add "HKCU\Software\Classes\abi-email" /v "URL Protocol" /d "" /f >nul
reg add "HKCU\Software\Classes\abi-email\DefaultIcon" /ve /d "%%SystemRoot%%\System32\shell32.dll,1" /f >nul
reg add "HKCU\Software\Classes\abi-email\shell\open\command" /ve /d "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%HELPER_DIR%\abi-email-helper.ps1\" \"%%1\"" /f >nul
echo ABI Email Helper installed successfully.
echo You can now use the Open Email button in ABI Rental.
pause
'''
    readme = f'''ABI Email Helper
================

This helper lets ABI Rental open generated Outlook .eml draft files the same way Windows opens a downloaded .eml file when you double-click it.

Install once on the office Windows PC:
1. Extract this ZIP file.
2. Double-click install-abi-email-helper.cmd.
3. If Windows asks for confirmation, allow it. No admin rights are required; it registers abi-email:// only for the current Windows user.
4. In ABI Rental, open an invoice or quote and click Open Email.

Security:
- The helper only downloads email drafts from abi-rental-platform.onrender.com, localhost, or 127.0.0.1.
- Drafts are saved temporarily under %TEMP%\\ABIEmailDrafts and opened with the Windows default .eml app, normally Outlook.
- If Open Email fails, use Generate Email in ABI Rental and open the downloaded .eml manually.

ABI Rental URL: {base_url}
'''
    return ps1, install_cmd, readme


def _public_base_url():
    configured = (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    if configured:
        return configured
    return request.url_root.rstrip("/")


def _email_context(document, settings, label=None, number=None):
    branch_email = document['branch_email'] or settings['email']
    branch_contact = document['branch_phone'] or settings['phone']
    return {
        'customer_name': document['customer_name'] or 'Customer',
        'document_label': label or display_document_label(document),
        'document_number': number or display_document_number(document),
        'order_number': document['order_number'],
        'company_name': settings['company_name'],
        'branch_name': document['branch_name'] or settings['company_name'],
        'branch_email': branch_email,
        'branch_contact': branch_contact,
        'branch_phone': branch_contact,
    }


def _invoice_email_logo_bytes(settings):
    if not settings['invoice_email_signature_include_logo']:
        return None
    logo_path = Path(current_app.static_folder) / DOCUMENT_LOGO_STATIC_PATH
    if not logo_path.exists():
        return None
    return logo_path.read_bytes()


def _prepare_email_draft(document_id, to_email, message=None):
    document = get_document(document_id)
    if not document:
        raise ValueError("Document not found")
    if document['document_type'] not in EMAIL_DRAFT_DOCUMENT_TYPES:
        raise ValueError("Email draft is available for invoices and quotes only")
    to_email = (to_email or document["customer_email"] or "").strip()
    if not to_email:
        raise ValueError("Customer email is required before preparing the email")
    settings = get_company_settings()
    if not settings:
        raise ValueError("Company settings not found")
    label = display_document_label(document)
    number = display_document_number(document)
    subject = build_invoice_email_subject(label, number, document['order_number'])
    context = _email_context(document, settings, label=label, number=number)
    body = message or render_email_template(settings['invoice_email_message'], context)
    signature = render_email_template(settings['invoice_email_signature'], context)
    pdf_bytes = document_pdf_bytes(document_id)
    pdf_filename = document_pdf_filename(document)
    eml_bytes = build_outlook_draft_eml(
        to_email,
        subject,
        body,
        pdf_bytes,
        pdf_filename,
        settings['email'],
        signature=signature,
        logo_bytes=_invoice_email_logo_bytes(settings),
    )
    return document, eml_bytes, f"EMAIL-{pdf_filename.rsplit('.', 1)[0]}.eml"

@bp.route("")
@login_required
def index():
    query = request.args.get("query", "").strip()
    document_type = request.args.get("document_type", "")
    status = request.args.get("status", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    documents = list_documents(query=query, document_type=document_type, status=status, start_date=start_date, end_date=end_date)
    # Compute totals for metrics
    docs_total = len(documents)
    docs_total_amount = sum((d["total"] or 0) for d in documents)
    return render_template(
        "admin/documents/index.html",
        settings=get_company_settings(),
        documents=documents,
        label_for=label_for,
        filter_counts=document_filter_counts(),
        docs_total=docs_total,
        docs_total_amount=docs_total_amount,
        filters={"query": query, "document_type": document_type, "status": status, "start_date": start_date, "end_date": end_date},
    )


@bp.route("/export.csv")
@login_required
def export_csv():
    query = request.args.get("query", "").strip()
    document_type = request.args.get("document_type", "")
    status = request.args.get("status", "")
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    documents = list_documents(query=query, document_type=document_type, status=status, start_date=start_date, end_date=end_date)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["number", "document_type", "order_number", "customer_name", "status", "total", "created_at"])
    for d in documents:
        writer.writerow([
            d["number"],
            d["document_type"],
            d["order_number"],
            d["customer_name"],
            d["status"],
            d["total"],
            d["created_at"],
        ])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=documents.csv"})


@bp.route("/<int:document_id>")
@login_required
def detail(document_id):
    document, items = printable_document(document_id)
    if not document:
        flash("Document not found", "error")
        return redirect(url_for("documents.index"))
    settings = get_company_settings()
    if not settings:
        flash("Company settings not found", "error")
        return redirect(url_for("documents.index"))
    email_message = render_email_template(
        settings['invoice_email_message'],
        _email_context(document, settings),
    )
    email_signature = render_email_template(settings['invoice_email_signature'], _email_context(document, settings))
    return render_template(
        "admin/documents/detail.html",
        settings=settings,
        document=document,
        items=items,
        label=label_for(document["document_type"]),
        display_label=display_document_label(document),
        display_number=display_document_number(document),
        custom_fields=custom_fields_for(document),
        document_date=document_date,
        document_datetime=document_datetime,
        rental_days_label=rental_days_label(document),
        email_message=email_message,
        email_signature=email_signature,
        email_signature_include_logo=bool(settings['invoice_email_signature_include_logo']),
    )


@bp.route("/<int:document_id>/download.pdf")
@login_required
def download_pdf(document_id):
    document = get_document(document_id)
    if not document:
        flash("Document not found", "error")
        return redirect(url_for("documents.index"))
    pdf_bytes = document_pdf_bytes(document_id)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={document_pdf_filename(document)}"},
    )


@bp.post("/orders/<int:order_id>")
@login_required
def create_for_order(order_id):
    try:
        document_id = create_document(order_id, request.form.get("document_type", ""))
        flash("Document created", "success")
        return redirect(url_for("documents.detail", document_id=document_id))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("orders.detail", order_id=order_id))


@bp.post("/<int:document_id>/finalize")
@login_required
def finalize(document_id):
    try:
        finalize_document(document_id)
        flash("Invoice finalized and numbered", "success")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("documents.detail", document_id=document_id))

@bp.route("/email-helper/download")
@login_required
def download_email_helper():
    ps1, install_cmd, readme = _email_helper_scripts(_public_base_url())
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("abi-email-helper.ps1", ps1)
        archive.writestr("install-abi-email-helper.cmd", install_cmd)
        archive.writestr("README.txt", readme)
    return Response(
        output.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=ABI-Email-Helper.zip"},
    )


@bp.post("/<int:document_id>/send-email")
@login_required
def send_document_email(document_id):
    try:
        document, eml_bytes, eml_name = _prepare_email_draft(
            document_id,
            request.form.get("to_email"),
            request.form.get("message"),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("documents.detail", document_id=document_id))
    mark_document_email(document_id, request.form.get("to_email") or document["customer_email"] or "", "prepared")
    return Response(
        eml_bytes,
        mimetype="message/rfc822",
        headers={"Content-Disposition": f"attachment; filename={eml_name}"},
    )


@bp.post("/<int:document_id>/open-email-link")
@login_required
def open_email_link(document_id):
    to_email = (request.form.get("to_email") or "").strip()
    message = request.form.get("message") or ""
    try:
        document, _eml_bytes, eml_name = _prepare_email_draft(document_id, to_email, message)
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    token = secrets.token_urlsafe(32)
    created_at = now()
    expires_at = (datetime.now() + timedelta(hours=EMAIL_HELPER_TOKEN_HOURS)).isoformat(timespec="seconds")
    resolved_to_email = to_email or document["customer_email"] or ""
    db = get_db()
    db.execute(
        "INSERT INTO email_open_tokens (token, document_id, to_email, message, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token, document_id, resolved_to_email, message, expires_at, created_at),
    )
    db.commit()
    mark_document_email(document_id, resolved_to_email, "prepared")
    download_url = f"{_public_base_url()}{url_for('documents.email_helper_draft', token=token)}"
    protocol_url = f"abi-email://open?url={quote(download_url, safe='')}&filename={quote(eml_name, safe='')}"
    return jsonify({"ok": True, "protocol_url": protocol_url, "download_url": download_url, "expires_at": expires_at})


@bp.route("/email-helper/<token>.eml")
def email_helper_draft(token):
    db = get_db()
    row = db.execute("SELECT * FROM email_open_tokens WHERE token = ?", (token,)).fetchone()
    if not row or row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
        return Response("Email helper link expired. Please generate a new email draft from ABI Rental.", status=410, mimetype="text/plain")
    try:
        _document, eml_bytes, eml_name = _prepare_email_draft(row["document_id"], row["to_email"], row["message"])
    except ValueError as exc:
        return Response(str(exc), status=404, mimetype="text/plain")
    return Response(
        eml_bytes,
        mimetype="message/rfc822",
        headers={"Content-Disposition": f"attachment; filename={eml_name}"},
    )
