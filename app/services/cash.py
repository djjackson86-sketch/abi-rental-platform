"""Day-end cash reconciliation for the dashboard (ticket ABI-341952951).

Five client asks, one small model:

1. an **opening cash** card, taken from the previous day's closing cash;
2. an option to **cash up** the cash a depot is holding (counted vs expected);
3. a **cash used** section, every line saying what the cash was spent on;
4. **end of day notes** that show on the dashboard and in the day report;
5. a **downloadable day report** for the dashboard figures.

Ticket ABI-341952952 adds a **cash drop off (to bank)** section: money taken out
of the drawer and banked during the day. It is amount-only on purpose ("Just the
amount" per the client), and it reduces what is expected to be left in the drawer
exactly like cash used does.

Cash up is **per depot per business day** — the app is branch-scoped everywhere
and each drawer cashes up its own day. The depot is the one this sign-in is
acting as (``session_primary_branch_id``), which for a branch-limited account is
its own branch or the depot it chose at sign-in, and it is always validated
against what the session may already reach, so nothing here can widen access.

Arithmetic, documented on screen as well as here:

    opening   = the last recorded closing cash for this depot on an earlier day
                (0 on a first-ever day, or before the previous day was cashed up)
    expected  = opening + cash payments received for the day - cash used
                - dropped off at the bank
    variance  = counted (closing) - expected

Every function returns **plain scalars and dicts**. Production rows are libsql
tuples, so a raw row object must never leave this module (``row.count`` is the
tuple method there, which is how /reports once 500'd in production only).
"""
from datetime import date

from app.db import get_db, now
from app.services.access import order_branch_clause, session_branch_scope_ids, session_primary_branch_id
from app.services.branches import branch_options
from app.services.timezone import display_local_datetime, local_now_iso


# The report is rendered on one PDF page, laid out in cards and panels
# (app/services/pdf_documents.py) — that layout budgets its own geometry exactly
# instead of counting lines. The only cap left here is how many end of day note
# lines the notes panel prints; the CSV export always carries every line, and the
# PDF states what it left out rather than dropping it silently.
MAX_NOTE_LINES_IN_PDF = 3


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
    except (KeyError, IndexError, TypeError, ValueError):
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


def cash_branches():
    """Depots this session may cash up, as ``[{'id', 'name'}]``.

    Never widened by anything in the request: an all-branch viewer gets every
    active depot, an account with a branch grant gets exactly its own depots, and
    a depot-restricted account gets one (so its panel shows a fixed label rather
    than a chooser).
    """
    scope = session_branch_scope_ids()
    options = []
    for row in branch_options():
        branch_id = _value(row, 'id')
        try:
            branch_id = int(branch_id)
        except (TypeError, ValueError):
            continue
        if scope is not None and branch_id not in scope:
            continue
        options.append({'id': branch_id, 'name': str(_value(row, 'name') or '')})
    return options


def branch_for_request(requested=''):
    """The depot a cash-up action applies to.

    A requested id is honoured only when it is one of the depots this session may
    already reach, so a crafted posted ``branch`` can narrow a cash up but never
    widen one. Anything else falls back to the depot the
    sign-in is acting as, then to the first depot that is allowed.
    """
    allowed = [branch['id'] for branch in cash_branches()]
    if not allowed:
        return None
    text = str(requested or '').strip()
    if text.isdigit() and int(text) in allowed:
        return int(text)
    primary = acting_branch_id()
    if primary in allowed:
        return primary
    return allowed[0]


def panel_state(requested=''):
    """Everything the dashboard's cash panel needs to pick a depot.

    ``branches`` is the chooser's options (never more than the session may
    reach), ``branch_id``/``branch_name`` are the depot the panel is showing.
    """
    branch_id = branch_for_request(requested)
    return {
        'branches': cash_branches(),
        'branch_id': branch_id,
        'branch_name': branch_name(branch_id) or '',
    }


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



def cash_deposit_refunds(day, branch_id):
    """Cash security-deposit refunds paid out on this business day.

    A cash refund physically leaves the drawer, so the expected closing cash must
    subtract it just like cash used and bank drops. The date follows the recorded
    deposit_processed_at timestamp because that is when the payout happened.
    """
    scope_sql, scope_params = order_branch_clause('o', branch_id=branch_id)
    row = get_db().execute(
        f"""SELECT COALESCE(SUM(o.deposit_refund_amount), 0) AS s
        FROM orders o
        WHERE LOWER(COALESCE(o.deposit_process_method, '')) = 'cash'
          AND COALESCE(o.deposit_refund_amount, 0) > 0
          AND substr(o.deposit_processed_at, 1, 10) = ?{scope_sql}""",
        [day, *scope_params],
    ).fetchone()
    return money(_value(row, 's'))

def _day_row(day, branch_id):
    if not branch_id:
        return None
    return get_db().execute(
        """SELECT id, opening_cash, counted_cash, notes, interaction_calls,
        interaction_whatsapp, interaction_emails, interaction_walk_in, interaction_notes
        FROM cash_ups WHERE branch_id = ? AND business_day = ?""",
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


def _drop_lines(cash_up_id):
    """The cash dropped off at the bank for this day, as plain dicts.

    Amount-only by design: the client asked for "just the amount", so there is
    deliberately no description column to fill in.
    """
    if not cash_up_id:
        return []
    lines = []
    for row in get_db().execute(
        'SELECT id, amount, created_at FROM cash_bank_drops WHERE cash_up_id = ? ORDER BY id',
        (cash_up_id,),
    ).fetchall():
        lines.append({
            'id': int(_value(row, 'id') or 0),
            'amount': money(_value(row, 'amount')),
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
    drop_lines = _drop_lines(cash_up_id)
    drop_total = money(sum(line['amount'] for line in drop_lines))
    opening_from, opening = _closing_before(day, branch_id)
    received = cash_received(day, branch_id) if branch_id else 0.0
    deposit_refund_total = cash_deposit_refunds(day, branch_id) if branch_id else 0.0
    # Cash banked, spent, or paid back as a deposit refund is no longer in the
    # drawer, so all three come off the expected figure.
    expected = money(opening + received - used_total - drop_total - deposit_refund_total)
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
        'drop_lines': drop_lines,
        'drop_count': len(drop_lines),
        'drop_total': drop_total,
        'deposit_refund_total': deposit_refund_total,
        'counted': counted_value,
        'cashed_up': cashed_up,
        'expected': expected,
        'variance': variance,
        'variance_display': _variance_text(variance, cashed_up),
        'variance_label': _variance_label(variance, cashed_up),
        'notes': str(_value(day_row, 'notes') or '') if day_row is not None else '',
        'interactions': {
            'calls': int(_value(day_row, 'interaction_calls', 0) or 0) if day_row is not None else 0,
            'whatsapp': int(_value(day_row, 'interaction_whatsapp', 0) or 0) if day_row is not None else 0,
            'emails': int(_value(day_row, 'interaction_emails', 0) or 0) if day_row is not None else 0,
            'walk_in': int(_value(day_row, 'interaction_walk_in', 0) or 0) if day_row is not None else 0,
            'notes': str(_value(day_row, 'interaction_notes') or '') if day_row is not None else '',
        },
        'has_day_row': day_row is not None,
    }


def aggregate_day_summary(day=None):
    """Read-only cash dashboard totals for every depot the session may see.

    Cash-up writes remain per depot. When the dashboard's top Branch filter is
    "All branches", this helper adds the same per-depot figures together but
    marks the result as non-writable so templates/routes do not offer a fake
    multi-depot save.
    """
    day = parse_business_day(day)
    branches = cash_branches()
    summaries = [day_summary(day=day, branch_id=branch['id']) for branch in branches]
    used_lines = []
    drop_lines = []
    interaction_notes = []
    notes = []
    counted_total = 0.0
    counted_count = 0
    for summary in summaries:
        for line in summary['used_lines']:
            item = dict(line)
            item['description'] = f"{summary['branch_name']}: {item['description']}"
            used_lines.append(item)
        for line in summary['drop_lines']:
            item = dict(line)
            item['branch_name'] = summary['branch_name']
            drop_lines.append(item)
        if summary.get('cashed_up'):
            counted_total += float(summary.get('counted') or 0)
            counted_count += 1
        if summary.get('notes'):
            notes.append(f"{summary['branch_name']}: {summary['notes']}")
        if summary.get('interactions', {}).get('notes'):
            interaction_notes.append(f"{summary['branch_name']}: {summary['interactions']['notes']}")

    opening = money(sum(summary['opening'] for summary in summaries))
    received = money(sum(summary['cash_received'] for summary in summaries))
    used_total = money(sum(summary['used_total'] for summary in summaries))
    drop_total = money(sum(summary['drop_total'] for summary in summaries))
    deposit_refund_total = money(sum(summary.get('deposit_refund_total', 0) for summary in summaries))
    expected = money(opening + received - used_total - drop_total - deposit_refund_total)
    cashed_up = counted_count > 0
    counted_value = money(counted_total) if cashed_up else None
    variance = money(counted_total - expected) if cashed_up else None
    all_cashed_up = bool(summaries) and counted_count == len(summaries)
    if cashed_up and not all_cashed_up:
        variance_label = f"Partial: {counted_count} of {len(summaries)} depots cashed up"
    else:
        variance_label = _variance_label(variance, cashed_up)
    return {
        'day': day,
        'branch_id': None,
        'branch_name': 'All branches',
        'branch_count': len(branches),
        'aggregate': True,
        'writable': False,
        'opening': opening,
        'opening_from': 'summed per depot' if branches else '',
        'cash_received': received,
        'used_lines': used_lines,
        'used_count': sum(summary['used_count'] for summary in summaries),
        'used_total': used_total,
        'drop_lines': drop_lines,
        'drop_count': sum(summary['drop_count'] for summary in summaries),
        'drop_total': drop_total,
        'deposit_refund_total': deposit_refund_total,
        'counted': counted_value,
        'cashed_up': cashed_up,
        'expected': expected,
        'variance': variance,
        'variance_display': _variance_text(variance, cashed_up),
        'variance_label': variance_label,
        'notes': '\n'.join(notes),
        'interactions': {
            'calls': sum(summary['interactions']['calls'] for summary in summaries),
            'whatsapp': sum(summary['interactions']['whatsapp'] for summary in summaries),
            'emails': sum(summary['interactions']['emails'] for summary in summaries),
            'walk_in': sum(summary['interactions']['walk_in'] for summary in summaries),
            'notes': '\n'.join(interaction_notes),
        },
        'has_day_row': any(summary.get('has_day_row') for summary in summaries),
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
        """INSERT INTO cash_ups (branch_id, business_day, opening_cash, counted_cash, notes,
        interaction_calls, interaction_whatsapp, interaction_emails, interaction_walk_in, interaction_notes,
        created_by, created_at, updated_at)
        VALUES (?, ?, ?, NULL, '', 0, 0, 0, 0, '', ?, ?, ?)""",
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


def _parse_interaction_count(value, label):
    text = str(value or '0').strip()
    if text == '':
        return 0
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be a whole number')
    if number < 0:
        raise ValueError(f'{label} cannot be negative')
    return number


def save_interactions(day, calls, whatsapp, emails, walk_in, notes='', branch_id=None, user_id=None):
    """Save the new-client interaction counts for one depot business day."""
    if branch_id is None:
        branch_id = acting_branch_id()
    if not branch_id:
        raise ValueError('No depot is available for client interactions')
    day = parse_business_day(day)
    values = {
        'calls': _parse_interaction_count(calls, 'Calls'),
        'whatsapp': _parse_interaction_count(whatsapp, 'WhatsApp'),
        'emails': _parse_interaction_count(emails, 'Emails'),
        'walk_in': _parse_interaction_count(walk_in, 'Walk-in'),
    }
    db = get_db()
    cash_up_id = _ensure_day(day, branch_id, user_id)
    db.execute(
        """UPDATE cash_ups SET interaction_calls = ?, interaction_whatsapp = ?,
        interaction_emails = ?, interaction_walk_in = ?, interaction_notes = ?,
        opening_cash = ?, updated_at = ? WHERE id = ?""",
        (values['calls'], values['whatsapp'], values['emails'], values['walk_in'],
         str(notes or '').strip(), opening_cash(day, branch_id), now(), cash_up_id),
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


def add_bank_drop(day, amount, branch_id=None, user_id=None):
    """Add one 'cash dropped off at the bank' line for the day.

    Amount only — the client asked for just the amount, so there is no
    description to capture. The line reduces the cash expected in the drawer.
    """
    if branch_id is None:
        branch_id = acting_branch_id()
    if not branch_id:
        raise ValueError('No depot is available to record a bank drop off')
    day = parse_business_day(day)
    value = _parse_amount(amount, 'Enter the amount dropped off at the bank')
    if value <= 0:
        raise ValueError('The amount dropped off at the bank must be more than zero')
    db = get_db()
    cash_up_id = _ensure_day(day, branch_id, user_id)
    db.execute(
        'INSERT INTO cash_bank_drops (cash_up_id, amount, created_at) VALUES (?, ?, ?)',
        (cash_up_id, value, now()),
    )
    db.commit()
    return cash_up_id


def delete_bank_drop(entry_id, branch_id=None):
    """Delete one bank drop-off line, refusing a line from another depot."""
    db = get_db()
    row = db.execute(
        """SELECT d.id AS id, c.branch_id AS branch_id FROM cash_bank_drops d
        JOIN cash_ups c ON c.id = d.cash_up_id WHERE d.id = ?""",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise ValueError('Bank drop off line not found')
    owner = int(_value(row, 'branch_id') or 0)
    if branch_id is not None and owner != int(branch_id):
        # Same wording as "not found": never confirm another depot's records.
        raise ValueError('Bank drop off line not found')
    db.execute('DELETE FROM cash_bank_drops WHERE id = ?', (entry_id,))
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
        ('Cash up', 'Total dropped at the bank', f"R{cash['drop_total']:.2f}"),
        ('Cash up', 'Cash deposit refunds paid out', f"R{cash.get('deposit_refund_total', 0):.2f}"),
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
    interactions = cash.get('interactions') or {}
    interaction_rows = [
        ('New client interactions', 'Calls', str(interactions.get('calls', 0) or 0)),
        ('New client interactions', 'WhatsApp', str(interactions.get('whatsapp', 0) or 0)),
        ('New client interactions', 'Emails', str(interactions.get('emails', 0) or 0)),
        ('New client interactions', 'Walk-in', str(interactions.get('walk_in', 0) or 0)),
        ('New client interactions', 'Notes', interactions.get('notes') or 'No interaction notes recorded'),
    ]
    rows = money_rows + cash_rows + interaction_rows
    for line in cash['used_lines']:
        rows.append(('Cash used', line['description'] or 'No description', f"R{line['amount']:.2f}"))
    if not cash['used_lines']:
        rows.append(('Cash used', 'No cash used recorded', 'R0.00'))
    # Amount-only lines: the timestamp is the only thing that tells two drop offs
    # on the same day apart, and it is what the dashboard panel shows too. It is
    # printed as the app prints every other date+time (YYYY-MM-DD  HH:MM) — the
    # stored ISO string's "T" has no place on a printed report or a CSV a depot
    # opens in Excel.
    for line in cash['drop_lines']:
        stamped = display_local_datetime(line['created_at']) if line['created_at'] else ''
        item = f'Dropped at the bank {stamped}'.strip()
        rows.append(('Cash drop off (to bank)', item, f"R{line['amount']:.2f}"))
    if not cash['drop_lines']:
        rows.append(('Cash drop off (to bank)', 'No cash dropped at the bank', 'R0.00'))
    notes = cash['notes'] or 'No end of day notes recorded'
    rows.append(('End of day notes', 'Notes', notes))
    return rows


def day_report_pdf_cards(report, user_name='', user_role=''):
    """The day report as the card sections the PDF draws.

    ``day_report_rows`` stays the one source of truth for every figure — this
    only regroups those rows into cards (the dashboard figures and the cash up)
    and panels (cash used, bank drop offs, end of day notes), so the PDF and the
    CSV export can never disagree about a number.

    Each list section carries every one of its lines plus the wording of the row
    that stands in for the lines that did not fit. The renderer knows how much of
    the single page is actually left and fills in the count, so a busy day states
    what it left out instead of dropping it silently.

    ``user_name``/``user_role`` come from the signed-in session and print in the
    header as "Prepared by: <name>" — the client asked for the report to say who
    ran the day.
    """
    cash = report['cash']
    grouped = {}
    for section, item, value in day_report_rows(report):
        grouped.setdefault(section, []).append((item, value))

    def cards_for(section_name):
        return [{'label': item, 'value': value} for item, value in grouped.get(section_name, [])]

    dashboard_cards = cards_for('Dashboard')
    cash_cards = cards_for('Cash up')
    # The variance is the figure a manager scans for, so it is the one card that
    # is allowed to change colour (green over/balanced, red short).
    tone = _variance_tone(cash)
    if tone:
        for card in cash_cards:
            if card['label'].startswith('Variance'):
                card['tone'] = tone

    notes_value = grouped.get('End of day notes', [('Notes', '')])[0][1]
    note_rows = [{'label': line} for line in ([line.strip() for line in str(notes_value).splitlines()] or [''])]

    prepared_by = str(user_name or '').strip()
    role_label = {'owner': 'Main profile', 'staff': 'Staff'}.get(str(user_role or '').strip())
    if prepared_by and role_label:
        prepared_by = f'{prepared_by} ({role_label})'

    sections = [
        {'kind': 'cards', 'title': 'Dashboard', 'cards': dashboard_cards},
        {'kind': 'cards', 'title': 'Cash up', 'cards': cash_cards},
    ]
    interaction_values = cash.get('interactions') or {}
    if any(int(interaction_values.get(key, 0) or 0) for key in ('calls', 'whatsapp', 'emails', 'walk_in')) or str(interaction_values.get('notes') or '').strip():
        sections.append({'kind': 'cards', 'title': 'New client interactions', 'cards': cards_for('New client interactions')})
    sections.extend([
        {
            'kind': 'list',
            'title': 'Cash used',
            'rows': _pdf_list_rows(grouped.get('Cash used', []), 'No cash used recorded'),
            'overflow': '... and {remaining} more cash used line(s) - see the CSV export',
        },
        {
            'kind': 'list',
            'title': 'Cash drop off (to bank)',
            'rows': _pdf_list_rows(grouped.get('Cash drop off (to bank)', []), 'No cash dropped at the bank'),
            'overflow': '... and {remaining} more bank drop off line(s) - see the CSV export',
        },
        {
            'kind': 'list',
            'title': 'End of day notes',
            'rows': note_rows,
            'cap': MAX_NOTE_LINES_IN_PDF,
            'overflow': '... more notes in the CSV export',
        },
    ])
    return {
        'meta': {
            'company': report['company'],
            'depot': cash['branch_name'],
            'day': cash['day'],
            'generated_at': report['generated_at'],
            'prepared_by': prepared_by,
        },
        # Section titles match the CSV export's own section names on purpose: the
        # two downloads are the same report, so they should read the same.
        'sections': sections,
    }


def _pdf_list_rows(rows, empty_label):
    """A list section's lines, or the one row that says nothing was recorded."""
    entries = [{'label': item, 'value': value} for item, value in rows if item != empty_label]
    return entries or [{'label': empty_label, 'value': ''}]


def _variance_tone(cash):
    """``short`` / ``over`` / ``balanced`` for the variance card, '' if open."""
    if not cash['cashed_up']:
        return ''
    if cash['variance'] < -0.005:
        return 'short'
    if cash['variance'] > 0.005:
        return 'over'
    return 'balanced'


def day_report_csv_rows(report):
    """(section, item, value) rows for the CSV export, uncapped."""
    return [list(row) for row in day_report_rows(report)]
