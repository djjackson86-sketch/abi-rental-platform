import os
import smtplib
import struct
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


def _image_dimensions(data):
    """Return (width, height) for a JPEG or PNG payload, else None.

    Mail clients differ wildly in how they treat CSS: Outlook's Word rendering
    engine ignores ``max-width`` entirely and falls back to the image's natural
    size (a 1200px logo then fills the whole message). Stamping explicit
    ``width``/``height`` attributes - derived from the real image - is the only
    sizing hint every client honours, so we read the dimensions from the bytes
    instead of hard-coding them for one particular logo file.
    """
    if not data:
        return None
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        if len(data) >= 24:
            return struct.unpack('>II', data[16:24])
        return None
    if data[:2] == b'\xff\xd8':
        index = 2
        length = len(data)
        while index + 9 < length:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            segment_length = struct.unpack('>H', data[index + 2:index + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack('>HH', data[index + 5:index + 9])
                return width, height
            index += 2 + segment_length
    return None


def signature_logo_html(logo_cid, display_width=180, data=None):
    """Build the signature <img> with sizing every mail client understands.

    ``width``/``height`` attributes carry the size for Outlook's Word engine,
    the inline style keeps the aspect ratio for WebKit/Blink clients and caps
    the logo on narrow phone screens, and ``display:block`` removes the extra
    baseline gap the signature paragraph would otherwise show.
    """
    dimensions = _image_dimensions(data)
    width = display_width
    height = round(display_width * dimensions[1] / dimensions[0]) if dimensions and dimensions[0] else None
    attributes = f'width="{width}"' + (f' height="{height}"' if height else '')
    style = (
        f'width:{width}px;max-width:{width}px;height:auto;display:block;border:0;'
        'outline:none;text-decoration:none;-ms-interpolation-mode:bicubic;'
    )
    return (
        f'<p class="email-signature-logo"><img src="cid:{escape(logo_cid)}" alt="SANO Trailers logo" '
        f'{attributes} style="{style}"></p>'
    )


def build_email_html(body, signature='', logo_cid=None, logo_data=None):
    html = ["<html><body>", _html_paragraphs(body)]
    if signature or logo_cid:
        html.append('<div class="email-signature">')
        if signature:
            html.append(_html_paragraphs(signature))
        if logo_cid:
            html.append(signature_logo_html(logo_cid, data=logo_data))
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
        # Keep the signature logo as a true inline related image, not a named
        # attachment. Outlook/Gmail-style clients display related parts with a
        # filename as separate attachments and can leave a broken image marker in
        # the signature body instead of resolving the cid.
        logo_cid = 'sano-trailers-email-logo' if logo_bytes else None
        msg.add_alternative(build_email_html(body, signature, logo_cid=logo_cid, logo_data=logo_bytes), subtype='html')
        if logo_bytes:
            payload = msg.get_payload()
            if isinstance(payload, list):
                html_part = payload[1]
                add_related = getattr(html_part, 'add_related')
                add_related(
                    logo_bytes,
                    maintype='image',
                    subtype='jpeg',
                    cid=f'<{logo_cid}>',
                    disposition='inline',
                )
    msg.add_attachment(attachment_bytes, maintype='application', subtype='pdf', filename=filename)
    return msg.as_bytes()
