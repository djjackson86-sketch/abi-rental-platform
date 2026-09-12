"""Order, quote and invoice numbering.

The client ran this business on Booqable, where order numbers had already
reached five digits (the last was around 10,118). The app therefore continues
that sequence instead of restarting at 1, and orders, quotes and invoices all
carry on from the same point.

NUMBERING_FLOOR is deliberately a floor and not a counter: numbering is derived
from the highest number actually stored, so the sequence keeps climbing after
normal use and never rewinds, while still starting at the client's number the
first time a document is issued.
"""

# Owner decision, 2026-09-12: continue the Booqable sequence, do not restart at 1.
NUMBERING_FLOOR = 10145


def number_value(value):
    """The numeric part of a stored number.

    Handles both this app's format and Booqable's bare numbers, so the two can
    live in the same sequence: 'ORD-00001' -> 1, '10118' -> 10118, '' -> 0.
    """
    digits = ''.join(character for character in str(value or '') if character.isdigit())
    return int(digits) if digits else 0


def next_in_sequence(existing_numbers, prefix):
    """Next number for a sequence, as '<prefix>-<nnnnn>'.

    Starts at the floor when nothing has been issued yet, and otherwise follows
    the highest number in use.
    """
    highest = max((number_value(value) for value in existing_numbers), default=0)
    return f"{prefix}-{max(highest + 1, NUMBERING_FLOOR):05d}"
