"""Day-end cash reconciliation for the dashboard (ticket ABI-341952951).

Five client asks, one small model:

1. an **opening cash** card, taken from the previous day's closing cash;
2. an option to **cash up** the cash a depot is holding (counted vs expected);
3. a **cash used** section, every line saying what the cash was spent on;
4. **end of day notes** that show on the dashboard and in the day report;
5. a **downloadable day report** for the dashboard figures.

Cash up is **per depot per business day** — the app is branch-scoped everywhere
and each drawer cashes up its own day. The depot is the one this sign-in is
acting as (``session_primary_branch_id``), which for a branch-limited account is
its own branch or the depot it chose at sign-in, and it is always validated
against what the session may already reach, so nothing here can widen access.

Arithmetic, documented on screen as well as here:

    opening   = the last recorded closing cash for this depot on an earlier day
                (0 on a first-ever day, or before the previous day was cashed up)
    expected  = opening + cash payments received for the day - cash used
    variance  = counted (closing) - expected

Every function returns **plain scalars and dicts**. Production rows are libsql
tuples, so a raw row object must never leave this module (``row.count`` is the
tuple method there, which is how /reports once 500'd in production only).
"""
from datetime import date

from app.db import get_db, now
from app.services.access import order_branch_clause, session_branch_scope_ids, session_primary_branch_id
from app.services.branches import branch_options
from app.services.timezone import local_now_iso


# The report is rendered on one PDF page with a fixed line budget; the CSV
# export always carries every line, so the PDF states what it left out rather
# than dropping it silently. 40 lines is inside what _simple_pdf prints from its
# starting y with 18pt leading.
MAX_NOTE_LINES_IN_PDF = 3
MAX_PDF_LINES = 40


def money(value):
    """Round a stored amount to cents, as the reports module does."""
    return round(float(value or 0), 2)


def today_iso():
    """Today's business day (Africa/Johannesburg), as YYYY-MM-DD."""
    return local_now_iso(timespec='seconds')[:10]


def parse_business_day(value):
    """A YYYY-MM-DD business day; a blank or junk value falls back to today.

    Falling back (rather than raising) keeps a crafted ``?day=`` from ever
    turning a read-only report or a cash-up POST into a 500.
    """
    text = str(value or '').strip()
    if len(text) >= 10:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            pass
    return today_iso()


def guard_writable_day(day):
    """Only today or an earlier day may be written to.

    Raises ``ValueError`` for a future day so a crafted hidden field cannot
    park a cash-up in a day that has not happened yet.
    """
    if str(day) > today_iso():
        raise ValueError('Cash up today or a past day only')
    return day


def _value(row, key, default=None):
    """Read one column off any of the three row shapes this app sees."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def acting_branch_id():
    """The depot this sign-in cashes up, or None when it may reach none.

    The main profile and all-branch accounts act as their own primary branch
    (or the first depot when they have none), a multi-depot account acts as the
    depot it chose at sign-in, and a single-depot account is pinned to its own
    branch. The result is always inside what the session may already reach, so
    this can narrow a session but never widen one.
    """
    scope = session_branch_scope_ids()
    primary = session_primary_branch_id()
    if scope is None:
        if primary:
            return primary
        branches = branch_options()
        return branches[0]['id'] if branches else None
    if not scope:
        return None
    if primary in scope:
        return primary
    return scope[0]


def branch_name(branch_id):
    if not branch_id:
        return ''
    row = get_db().execute('SELECT name FROM branches WHERE id = ?', (branch_id,)).fetchone()
    return str(_value(row, 'name') or '')


def _closing_before(day, branch_id):
    """(day, closing) of the last cashed-up day before ``day``, else (``''``, 0.0)."""
    if not branch_id:
        return '', 0.0
    row = get_db().execute(
        """SELECT business_day, counted_cash FROM cash_ups
        WHERE branch_id = ? AND business_day < ? AND counted_cash IS NOT NULL
        ORDER BY business_day DESC LIMIT 1""",
        (branch_id, day),
    ).fetchone()
    if row is None:
        return '', 0.0
    return str(_value(row, 'business_day') or ''), money(_value(row, 'counted_cash'))


def opening_cash(day, branch_id):
    """Opening cash for the day: the previous day's recorded closing cash."""
    return _closing_before(day, branch_id)[1]


def cash_received(day, branch_id):
    """Cash payments received for the day, mirroring the dashboard cash card.

    Same filters as the ``Total cash payments`` card: paid, not archived, and
    dated by ``payment_date`` falling back to ``created_at``. Restricted to the
    cashing-up depot, and the session scope still wins over that restriction.
    """
    scope_sql, scope_params = order_branch_clause('o', branch_id=branch_id)
    row = get_db().execute(
        f"""SELECT COALESCE(SUM(pay.amount), 0) AS s
        FROM payments pay JOIN orders o ON o.id = pay.order_id
        WHERE pay.status = 'paid' AND COALESCE(pay.deleted_at, '') = ''
          AND LOWER(pay.method) = 'cash'
          AND substr(COALESCE(NULLIF(pay.payment_date, ''), pay.created_at), 1, 10) = ?{scope_sql}""",
        [day, *scope_params],
    ).fetchone()
    return money(_value(row, 's'))


def _day_row(day, branch_id):
    if not branch_id:
        return None
    return get_db().execute(
        'SELECT id, opening_cash, counted_cash, notes FROM cash_ups WHERE branch_id = ? AND business_day = ?',
        (branch_id, day),
    ).fetchone()


def _used_lines(cash_up_id):
    if not cash_up_id:
        return []
    lines = []
    for row in get_db().execute(
        'SELECT id, amount, description, created_at FROM cash_used WHERE cash_up_id = ? ORDER BY id',
        (cash_up_id,),
    ).fetchall():
        lines.append({
            'id': int(_value(row, 'id') or 0),
            'amount': money(_value(row, 'amount')),
            'description': str(_value(row, 'description') or ''),
            'created_at': str(_value(row, 'created_at') or ''),
        })
    return lines


def _variance_text(variance, cashed_up):
    if not cashed_up:
        return '—'
    if variance > 0.005:
        return f'+R{abs(variance):.2f}'
    if variance < -0.005:
        return f'-R{abs(variance):.2f}'
    return 'R0.00'


def _variance_label(variance, cashed_up):
    if not cashed_up:
        return 'Cash up to see the difference'
    if variance > 0.005:
        return f'Over by R{abs(variance):.2f}'
    if variance < -0.005:
        return f'Short by R{abs(variance):.2f}'
    return 'Balanced'


def day_summary(day=None, branch_id=None):
    """Everything the dashboard's cash panel and the day report need.

    Plain values only — see the module docstring.
    """
    day = parse_business_day(day)
    if branch_id is None:
        branch_id = acting_branch_id()
    if branch_id is not None:
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            branch_id = None

    day_row = _day_row(day, branch_id)
    cash_up_id = int(_value(day_row, 'id') or 0) if day_row is not None else 0
    counted = _value(day_row, 'counted_cash') if day_row is not None else None
    cashed_up = day_row is not None and counted is not None
    counted_value = money(counted) if cashed_up else None
    used_lines = _used_lines(cash_up_id)
    used_total = money(sum(line['amount'] for line in used_lines))
    opening_from, opening = _closing_before(day, branch_id)
    received = cash_received(day, branch_id) if branch_id else 0.0
    expected = money(opening + received - used_total)
    variance = None
    if cashed_up:
        variance = money(float(counted_value or 0) - expected)
    return {
        'day': day,
        'branch_id': branch_id,
        'branch_name': branch_name(branch_id) or 'this depot',
        'opening': opening,
        'opening_from': opening_from,
        'cash_received': received,
        'used_lines': used_lines,
        'used_count': len(used_lines),
        'used_total': used_total,
        'counted': counted_value,
        'cashed_up': cashed_up,
        'expected': expected,
        'variance': variance,
        'variance_display': _variance_text(variance, cashed_up),
        'variance_label': _variance_label(variance, cashed_up),
        'notes': str(_value(day_row, 'notes') or '') if day_row is not None else '',
        'has_day_row': day_row is not None,
    }


def _ensure_day(day, branch_id, user_id=None):
    """The (depot, day) row id, creating it with an opening snapshot if needed.

    Creating the row up front is what lets cash-used lines and notes be captured
    before the drawer is counted — ``counted_cash`` stays NULL until the cash up.
    """
    db = get_db()
    existing = _day_row(day, branch_id)
    if existing is not None:
        return int(_value(existing, 'id') or 0)
    opening = opening_cash(day, branch_id)
    ts = now()
    db.execute(
        """INSERT INTO cash_ups (branch_id, business_day, opening_cash, counted_cash, notes, created_by, created_at, updated_at)
        VALUES (?, ?, ?, NULL, '', ?, ?, ?)""",
        (branch_id, day, opening, user_id, ts, ts),
    )
    db.commit()
    row = _day_row(day, branch_id)
    return int(_value(row, 'id') or 0)


def _parse_amount(value, message):
    text = str(value or '').replace(',', '').strip()
    try:
        amount = round(float(text), 2)
    except (TypeError, ValueError):
        raise ValueError(message)
    return amount


def save_cash_up(day, counted_cash, notes='', branch_id=None, user_id=None):
    """Record the counted (closing) cash for the day — the actual cash up.

    Re-saving the same day updates the count and refreshes the opening snapshot
    from the previous day's closing cash, so entering yesterday's count late
    still leaves today's opening honest.
    """
    if branch_id is None:
        branch_id = acting_branch_id()
    if not branch_id:
        raise ValueError('No depot is available to cash up')
    day = parse_business_day(day)
    counted = _parse_amount(counted_cash, 'Enter the cash counted for the day')
    if counted < 0:
        raise ValueError('Counted cash cannot be negative')
    db = get_db()
    cash_up_id = _ensure_day(day, branch_id, user_id)
    db.execute(
        'UPDATE cash_ups SET counted_cash = ?, opening_cash = ?, notes = ?, updated_at = ? WHERE id = ?',
        (counted, opening_cash(day, branch_id), str(notes or '').strip(), now(), cash_up_id),
    )
    db.commit()
    return cash_up_id


def save_notes(day, notes, branch_id=None, user_id=None):
    """Save the end of day notes without touching the counted cash."""
    if branch_id is None:
        branch_id = acting_branch_id()
    if not branch_id:
        raise ValueError('No depot is available for end of day notes')
    day = parse_business_day(day)
    db = get_db()
    cash_up_id = _ensure_day(day, branch_id, user_id)
    db.execute(
        'UPDATE cash_ups SET notes = ?, opening_cash = ?, updated_at = ? WHERE id = ?',
        (str(notes or '').strip(), opening_cash(day, branch_id), now(), cash_up_id),
    )
    db.commit()
    return cash_up_id


def add_cash_used(day, amount, description, branch_id=None, user_id=None):
    """Add one 'cash used' line, with the description of what it was for."""
    if branch_id is None:
        branch_id = acting_branch_id()
    if not branch_id:
        raise ValueError('No depot is available to record cash used')
    day = parse_business_day(day)
    value = _parse_amount(amount, 'Enter the amount of cash used')
    if value <= 0:
        raise ValueError('The amount of cash used must be more than zero')
    text = str(description or '').strip()
    if not text:
        raise ValueError('Describe what the cash was used for')
    db = get_db()
    cash_up_id = _ensure_day(day, branch_id, user_id)
    db.execute(
        'INSERT INTO cash_used (cash_up_id, amount, description, created_at) VALUES (?, ?, ?, ?)',
        (cash_up_id, value, text[:200], now()),
    )
    db.commit()
    return cash_up_id


def delete_cash_used(entry_id, branch_id=None):
    """Delete one cash-used line, refusing a line that belongs to another depot."""
    db = get_db()
    row = db.execute(
        """SELECT cu.id AS id, c.branch_id AS branch_id FROM cash_used cu
        JOIN cash_ups c ON c.id = cu.cash_up_id WHERE cu.id = ?""",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise ValueError('Cash used line not found')
    owner = int(_value(row, 'branch_id') or 0)
    if branch_id is not None and owner != int(branch_id):
        # Same wording as "not found": never confirm another depot's records.
        raise ValueError('Cash used line not found')
    db.execute('DELETE FROM cash_used WHERE id = ?', (entry_id,))
    db.commit()
    return owner


def day_report(day=None, branch_id=None):
    """The day's dashboard figures plus its cash reconciliation.

    The dashboard metrics keep the dashboard's own branch scope (the session),
    while the cash half follows the cashing-up depot — which for a scoped
    account is the same depot, so the two halves agree.
    """
    from app.services.reports import dashboard_day_metrics
    from app.services.settings import get_company_settings

    summary = day_summary(day=day, branch_id=branch_id)
    settings = get_company_settings() or {}
    return {
        'company': str(_value(settings, 'company_name') or ''),
        'generated_at': now(),
        'cash': summary,
        'metrics': dashboard_day_metrics(summary['day']),
    }


def day_report_rows(report):
    """One source of truth for both the PDF and the CSV: (section, item, value)."""
    cash = report['cash']
    metrics = report['metrics']
    money_rows = [
        ('Dashboard', 'New orders for the day', str(metrics['orders'])),
        ('Dashboard', 'New customers for the day', str(metrics['customers'])),
        ('Dashboard', 'Revenue for the day', f"R{metrics['revenue']:.2f}"),
        ('Dashboard', 'Total card payments', f"R{metrics['card_payments']:.2f}"),
        ('Dashboard', 'Total cash payments', f"R{metrics['cash_payments']:.2f}"),
        ('Dashboard', 'Total EFT payments', f"R{metrics['eft_payments']:.2f}"),
        ('Dashboard', 'Reservations for the day', str(metrics['reservations'])),
        ('Dashboard', 'Reservation pick ups for the day', str(metrics['reservation_pickups'])),
        ('Dashboard', 'Total no. of trailers out', str(metrics['trailers_out'])),
        ('Dashboard', 'Total no. of trailers in', str(metrics['trailers_in'])),
    ]
    opening_item = 'Opening cash (previous day closing)'
    opening_value = f"R{cash['opening']:.2f}"
    if cash['opening_from']:
        opening_value += f" (closed {cash['opening_from']})"
    cash_rows = [
        ('Cash up', opening_item, opening_value),
        ('Cash up', 'Cash received for the day', f"R{cash['cash_received']:.2f}"),
        ('Cash up', 'Cash used for the day', f"R{cash['used_total']:.2f}"),
        ('Cash up', 'Expected cash in the drawer', f"R{cash['expected']:.2f}"),
        (
            'Cash up',
            'Closing cash (counted)',
            f"R{cash['counted']:.2f}" if cash['cashed_up'] else 'Not cashed up yet',
        ),
        (
            'Cash up',
            'Variance (counted - expected)',
            f"{cash['variance_display']} ({cash['variance_label']})" if cash['cashed_up'] else 'Not cashed up yet',
        ),
    ]
    rows = money_rows + cash_rows
    for line in cash['used_lines']:
        rows.append(('Cash used', line['description'] or 'No description', f"R{line['amount']:.2f}"))
    if not cash['used_lines']:
        rows.append(('Cash used', 'No cash used recorded', 'R0.00'))
    notes = cash['notes'] or 'No end of day notes recorded'
    rows.append(('End of day notes', 'Notes', notes))
    return rows


def day_report_pdf_lines(report):
    """The report lines for the PDF, fitted to the single page it renders on.

    ``_simple_pdf`` writes exactly one page, so the budget is worked out here
    rather than discovered by silent truncation: the cash-used lines are capped
    first and the number left out is stated on the page, while the CSV export
    always carries every line. The notes are free text and are printed as their
    own lines, so a newline can never land inside a PDF string literal.
    """
    cash = report['cash']
    grouped = {}
    for section, item, value in day_report_rows(report):
        grouped.setdefault(section, []).append((item, value))
    dashboard_lines = [f'{item}: {value}' for item, value in grouped.get('Dashboard', [])]
    cash_lines = [f'{item}: {value}' for item, value in grouped.get('Cash up', [])]
    used_entries = [
        f'{item}: {value}' for item, value in grouped.get('Cash used', [])
        if item != 'No cash used recorded'
    ]
    notes_value = grouped.get('End of day notes', [('Notes', '')])[0][1]
    note_lines = [line.strip() for line in str(notes_value).splitlines()] or ['']

    # Blank line + section title per section, plus the header block.
    fixed = 5 + (2 + len(dashboard_lines)) + (2 + len(cash_lines)) + 2 + 2
    budget = max(0, MAX_PDF_LINES - fixed)
    notes_shown = min(len(note_lines), MAX_NOTE_LINES_IN_PDF, max(1, budget - 1))
    used_shown = min(len(used_entries), max(0, budget - notes_shown))
    truncated_used = len(used_entries) - used_shown
    if truncated_used:
        # The "and N more" line itself needs a slot.
        used_shown = max(0, used_shown - 1)
        truncated_used = len(used_entries) - used_shown

    header = [
        f"{report['company']} - daily dashboard report".strip(' -'),
        f"Depot: {cash['branch_name']}",
        f"Business day: {cash['day']}",
        f"Generated: {report['generated_at']}",
        '',
        'DASHBOARD',
    ]
    lines = header + dashboard_lines + ['', 'CASH UP'] + cash_lines + ['', 'CASH USED']
    lines.extend(used_entries[:used_shown])
    if truncated_used:
        lines.append(f'... and {truncated_used} more cash used line(s) - see the CSV export')
    lines.extend(['', 'END OF DAY NOTES'])
    if len(note_lines) > notes_shown:
        lines.extend(note_lines[:max(0, notes_shown - 1)])
        lines.append('... more notes in the CSV export')
    else:
        lines.extend(note_lines[:notes_shown])
    return lines[:MAX_PDF_LINES]


def day_report_csv_rows(report):
    """(section, item, value) rows for the CSV export, uncapped."""
    return [list(row) for row in day_report_rows(report)]
