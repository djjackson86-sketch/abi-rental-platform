"""Human-readable money only; never use for stored or machine-readable amounts."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import current_app, has_app_context


def format_amount(value):
    """Space-grouped thousands, decimal point and exactly two places."""
    try:
        amount = Decimal(str(value or 0))
        if not amount.is_finite():
            amount = Decimal(0)
        amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal(0)
    # Avoid a misleading minus on balances that round to zero.
    if not amount:
        amount = abs(amount)
    return format(amount, ',.2f').replace(',', ' ')


def format_currency(value, symbol=None):
    if symbol is None:
        symbol = current_app.config.get('CURRENCY_SYMBOL', 'R') if has_app_context() else 'R'
    amount = format_amount(value)
    return '-' + symbol + amount[1:] if amount.startswith('-') else symbol + amount
