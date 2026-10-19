"""Company document-copy recipients (ABI-341953239). No delivery side effects."""
from email.headerregistry import Address
from email.errors import MessageError


def additional_emails(value, primary=''):
    """Parse semicolon-separated bare addresses; reject unsafe/ambiguous input."""
    value = str(value or '')
    if any(ord(char) < 32 or ord(char) == 127 or char in '\u0085\u2028\u2029' for char in value):
        raise ValueError('Other email addresses must not contain line breaks or control characters')
    result = []
    seen = {str(primary or '').strip().lower()}
    for part in value.split(';'):
        address = part.strip()
        if not address:
            continue
        try:
            parsed = Address(addr_spec=address)
            if not parsed.username or not parsed.domain or parsed.addr_spec.lower() != address.lower() or any(c.isspace() for c in address):
                raise ValueError()
        except (ValueError, IndexError, MessageError) as exc:
            raise ValueError(f'Invalid other email address: {address}') from exc
        if address.lower() not in seen:
            seen.add(address.lower())
            result.append(address.lower())
    return result


def document_copy_emails(document, office_email, to_email=''):
    from app.services.customers import raw_custom_fields_for
    copies = [office_email.strip()]
    if document['customer_type'] == 'company':
        copies.extend(additional_emails(raw_custom_fields_for(document).get('other_email_addresses'), document['customer_email']))
    seen = {str(to_email or '').strip().lower()}
    result = []
    for address in copies:
        if address and address.lower() not in seen:
            seen.add(address.lower())
            result.append(address)
    return ', '.join(result)
