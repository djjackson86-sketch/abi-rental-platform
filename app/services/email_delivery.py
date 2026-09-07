import os
import smtplib
from html import escape
from email.message import EmailMessage
from email.utils import formatdate


def email_configured():
    return bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM_EMAIL'))


def send_email_with_attachment(to_email, subject, body, attachment_bytes, filename):
    if not email_configured():
        raise RuntimeError('Email provider not configured')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = os.environ['SMTP_FROM_EMAIL']
    msg['To'] = to_email
    if os.environ.get('SMTP_REPLY_TO'):
        msg['Reply-To'] = os.environ['SMTP_REPLY_TO']
    msg.set_content(body)
    msg.add_attachment(attachment_bytes, maintype='application', subtype='pdf', filename=filename)
    host = os.environ['SMTP_HOST']
    port = int(os.environ.get('SMTP_PORT') or 587)
    username = os.environ.get('SMTP_USERNAME')
    password = os.environ.get('SMTP_PASSWORD')
    use_tls = os.environ.get('SMTP_USE_TLS', '1').lower() not in {'0', 'false', 'no'}
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        if use_tls:
            smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)


def render_email_template(template, context):
    text = template or ''
    for key, value in context.items():
        text = text.replace('{' + key + '}', str(value or ''))
    return text


def build_invoice_email_subject(document_label, document_number, order_number):
    number_part = f" {document_number}" if document_number else ""
    return f"{document_label}{number_part} for order {order_number}"


def combine_email_body(body, signature=''):
    body = (body or '').strip()
    signature = (signature or '').strip()
    if body and signature:
        return f"{body}\n\n{signature}"
    return body or signature


def _html_paragraphs(text):
    paragraphs = []
    for block in (text or '').strip().split('\n\n'):
        lines = [escape(line) for line in block.splitlines()]
        if lines:
            paragraphs.append(f"<p>{'<br>'.join(lines)}</p>")
    return ''.join(paragraphs)


def build_email_html(body, signature='', logo_cid=None):
    html = ["<html><body>", _html_paragraphs(body)]
    if signature or logo_cid:
        html.append('<div class="email-signature">')
        if signature:
            html.append(_html_paragraphs(signature))
        if logo_cid:
            html.append(f'<p><img src="cid:{escape(logo_cid)}" alt="SANO Trailers logo" style="max-width:180px;height:auto;"></p>')
        html.append('</div>')
    html.append("</body></html>")
    return ''.join(html)


def build_outlook_draft_eml(to_email, subject, body, attachment_bytes, filename, from_email='', cc_email='', signature='', logo_bytes=None, logo_filename='sano-trailers-logo.jpg'):
    """Build an RFC 822 .eml file that Outlook/default mail apps can open.

    Browsers cannot safely launch a desktop compose window with a file attachment via
    mailto. Downloading/opening this .eml gives staff a prepared message with the PDF
    invoice attached so they can review it in Outlook before clicking Send. The office
    address (cc_email) is always included on Cc so every generated draft keeps the
    SANO Trailers office informed.

    The file is deliberately not pre-stamped with a From sender and carries
    X-Unsent: 1, so Outlook opens it as a draft that sends from the current user's
    own account instead of a fixed office address.
    """
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['X-Unsent'] = '1'
    if from_email:
        msg['From'] = from_email
    msg['To'] = to_email
    if cc_email:
        msg['Cc'] = cc_email
    msg['Date'] = formatdate(localtime=True)
    msg.set_content(combine_email_body(body, signature))
    if signature or logo_bytes:
        logo_cid = 'invoice-signature-logo@abi-rental-platform' if logo_bytes else None
        msg.add_alternative(build_email_html(body, signature, logo_cid=logo_cid), subtype='html')
        if logo_bytes:
            payload = msg.get_payload()
            if isinstance(payload, list):
                html_part = payload[1]
                add_related = getattr(html_part, 'add_related')
                add_related(logo_bytes, maintype='image', subtype='jpeg', cid=f'<{logo_cid}>', filename=logo_filename)
    msg.add_attachment(attachment_bytes, maintype='application', subtype='pdf', filename=filename)
    return msg.as_bytes()
