import os
import smtplib
from email.message import EmailMessage


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
