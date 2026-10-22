"""Read-only paper-account reporting. No trading classes or API calls needed."""
# Import calendar scheduling, report aggregation, JSON, and read-only database utilities.
from collections import Counter
from datetime import datetime, timedelta, time, timezone
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

# Use UTC as the common timezone for stored timestamps and report comparisons.
UTC = timezone.utc


# Parse an ISO timestamp and normalize it to UTC.
def stamp(value):

    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))

    # Reject timestamps without a timezone to prevent ambiguous report boundaries.
    if value.tzinfo is None:
        raise ValueError('Database timestamps must include a timezone.')
    
    return value.astimezone(UTC)


# Load report settings and validate the timezone and bounded integer options.
def load_settings(path):
    settings = json.loads(Path(path).read_text(encoding='utf-8'))
    ZoneInfo(settings['timezone'])

    # Define permitted ranges for scheduling, polling, retry, and valuation coverage settings.
    bounds = {'weekday': (0, 6), 'hour': (0, 23), 'minute': (0, 59),
              'poll_seconds': (1, 3600), 'retry_seconds': (1, 86400),
              'max_attempts': (1, 100), 'max_quote_age_seconds': (1, 86400),
              'max_quote_gap_seconds': (1, 86400)}
    
    # Validate every bounded setting against its required integer range.
    for key, (lo, hi) in bounds.items():

        # Reject missing, non-integer, or out-of-range settings, including Boolean substitutes.
        if type(settings.get(key)) is not int or not lo <= settings[key] <= hi:
            raise ValueError(f'{key} must be an integer from {lo} to {hi}.')
        
    return settings


# Find the most recent scheduled local report time, including overdue reports after restart.
def latest_due(now, settings):
    """Latest scheduled local date/time, including catch-up after a restart."""
    zone = ZoneInfo(settings['timezone'])
    # Calculate the configured weekday and local send time for the current calendar week.
    local = now.astimezone(zone)
    day = local.date() - timedelta(days = (local.weekday() - settings['weekday']) % 7)
    due = datetime.combine(day, 
                           time(settings['hour'], 
                                settings['minute']), 
                                zone)

    # Move back one week when this week's scheduled send time has not yet arrived.
    if due > local:
        due = datetime.combine(day - timedelta(days = 7), 
                               due.timetz().replace(tzinfo = None), 
                               zone)

    return due


# Return UTC bounds for a completed calendar week or a current week-to-date preview.
def report_period(now, settings, current=False):
    """Calendar-week bounds; DST weeks need not contain exactly 168 hours."""
    # Locate midnight on the configured start weekday in the reporting timezone.
    zone = ZoneInfo(settings['timezone'])
    local = now.astimezone(zone)
    day = local.date() - timedelta(days = (local.weekday() - settings['weekday']) % 7)
    end = datetime.combine(day, time.min, zone)
    # For previews, use the current time as the end of the ongoing reporting week.
    if current:
        return end.astimezone(UTC), now.astimezone(UTC)
    # For completed reports, use the preceding local calendar week, preserving daylight-saving changes.
    return (datetime.combine(day - timedelta(days = 7), time.min, zone).astimezone(UTC),
            end.astimezone(UTC))


# Determine the reporting week associated with the most recent scheduled send time.
def due_period(now, settings):
    due = latest_due(now, settings)
    return report_period(due, settings)


# Read a consistent paper-account snapshot and calculate report metrics with coverage warnings.
def read_report(database, start, end, settings):
    """Value changes assume the existing engine has no deposits/withdrawals.

    Use quote snapshots ONLY. Close-decision snapshots are labelled with the
    candle opening time in this project and cannot be treated as live valuations.
    All reads share one SQLite transaction for a consistent view.
    """
    path = Path(database).resolve()

    # Reject a missing database file before attempting read-only access.
    if not path.is_file():
        raise ValueError('Paper database does not exist; check --db.')
    
    # Require a nonempty reporting interval with the end after the start.
    if start >= end:
        raise ValueError('Report end must follow start.')
    
    # Open the account database in read-only mode and enable named-column access.
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row

    # Read account mode, quote snapshots, and trades within one consistent transaction.
    try:
        db.execute('BEGIN')

        checkpoint = db.execute('SELECT mode FROM checkpoint WHERE id=1').fetchone()

        # Require an initialized paper account rather than a backtest database.
        if checkpoint is None or checkpoint['mode'] != 'paper':
            raise ValueError('Reports require an initialized PAPER database.')
        
        # SQLite handles both ISO timestamps and the pandas space-separated form.
        # Find the latest quote valuation strictly before the period starts.
        previous = db.execute("""SELECT * FROM snapshots WHERE event_key LIKE 'quote:%'
            AND julianday(timestamp) < julianday(?)
            ORDER BY julianday(timestamp) DESC, rowid DESC LIMIT 1""",
            (start.isoformat(),)).fetchone()
        
        # Read quote snapshots around the reporting boundaries in chronological order.
        rows = db.execute("""SELECT * FROM snapshots WHERE event_key LIKE 'quote:%'
            AND julianday(timestamp) >= julianday(?) AND julianday(timestamp) <= julianday(?)
            ORDER BY julianday(timestamp), rowid""", (start.isoformat(), end.isoformat())).fetchall()
        
        # Read trade history through the period end for weekly totals and closing cost-basis reconstruction.
        trades = db.execute('SELECT * FROM trades WHERE julianday(timestamp) <= julianday(?) ORDER BY julianday(timestamp), rowid',
                            (end.isoformat(),)).fetchall()
    # Close the database connection regardless of query or validation failures.
    finally:
        db.close()

    # Apply the exact start-inclusive and end-exclusive interval to quote snapshots.
    snapshots = [dict(row) for row in rows if start <= stamp(row['timestamp']) < end]

    # Use the preceding valuation as the opening and the latest in-period quote as the closing.
    opening = dict(previous) if previous else None

    closing = snapshots[-1] if snapshots else opening

    # Select only trades within the reporting period for weekly activity metrics.
    weekly = [dict(row) for row in trades if start <= stamp(row['timestamp']) < end]

    # Prepare coverage warnings and calculate the ages of the opening and closing valuations.
    warnings = []
    max_age = settings['max_quote_age_seconds']

    opening_age = (start-stamp(opening['timestamp'])).total_seconds() if opening else None
    closing_age = (end-stamp(closing['timestamp'])).total_seconds() if closing else None

    # Warn when the opening valuation is missing or older than the allowed threshold.
    if opening is None or opening_age > max_age:
        warnings.append('Opening valuation unavailable/stale: weekly change and return are N/A.')

    # Warn when the closing valuation is missing or older than the allowed threshold.
    if closing is None or closing_age > max_age:
        warnings.append('Closing valuation unavailable/stale: weekly change and return are N/A.')

    # Calculate change and return only when both endpoint valuations are fresh and available.
    usable = opening is not None and closing is not None and opening_age <= max_age and closing_age <= max_age
    change = closing['portfolio_value'] - opening['portfolio_value'] if usable else None

    return_pct = 100 * change/opening['portfolio_value'] if usable and opening['portfolio_value'] > 0 else None
    observed = ([opening] if opening else []) + snapshots

    # Track observed valuation peaks and the largest percentage decline from a prior peak.
    peak = None
    drawdown = 0.0

    # Process each recorded valuation in chronological order to calculate sampled drawdown.
    for row in observed:
        v = row['portfolio_value']

        # Reject negative or non-finite portfolio valuations.
        if not math.isfinite(v) or v < 0:
            raise ValueError('Invalid portfolio value in snapshots.')
        
        # Update the running peak and largest observed decline, avoiding division by a zero peak.
        peak = v if peak is None else max(peak, v)

        drawdown = max(drawdown, (peak - v) / peak * 100 if peak else 0)
    # Measure the largest gap between report boundaries and recorded quote times.
    points = [start] + [stamp(row['timestamp']) for row in snapshots] + [end]
    largest_gap = max((b-a).total_seconds() for a, b in zip(points, points[1:]))

    # Warn when quote coverage exceeds the configured maximum gap.
    if largest_gap > settings['max_quote_gap_seconds']:
        warnings.append('Quote coverage has gaps; drawdown uses recorded observations only.')

    # Disclose execution uncertainty if any included account snapshot carries that flag.
    if any(json.loads(row['payload_json']).get('execution_path_uncertain', False) for row in observed):
        warnings.append('Account has execution_path_uncertain flagged; results are a paper continuation scenario.')
    
    # Warn about explicitly stale snapshots and suppress the period change and return.
    if any(json.loads(row['payload_json']).get('valuation_is_stale', False) for row in observed):
        warnings.append('A recorded snapshot is flagged as stale.')
        change = return_pct = None
    # Reconstruct remaining cost basis up to the actual closing quote. Trade
    # net_usd already includes fees; do not subtract fees again from P/L.
    unrealized = None
    # Reconstruct remaining cost basis only when a closing valuation is available.
    if closing is not None:
        remaining_cost = 0.0

        # Accumulate the cost of holdings using trades no later than the actual closing quote.
        for row in trades:
            # Skip trades that occurred after the selected closing valuation.
            if stamp(row['timestamp']) > stamp(closing['timestamp']):
                continue

            fill = json.loads(row['fill_json'])

            # Add fee-inclusive purchase outlays to the remaining cost basis.
            if row['side'] == 'BUY':
                remaining_cost += fill['net_usd']

            # Remove the sold cost basis using net sale proceeds minus realized profit or loss.
            elif row['side'] == 'SELL' and row['realized_pnl'] is not None:
                remaining_cost -= fill['net_usd']-row['realized_pnl']

            # Reject trades whose accounting data cannot support cost-basis reconstruction.
            else:
                raise ValueError('Cannot reconstruct cost basis for a trade.')
            
        # Subtract cash and remaining cost from closing portfolio value to obtain unrealized profit or loss.
        unrealized = closing['portfolio_value'] - closing['cash'] - remaining_cost

    # Count weekly fills by strategy and side, and count sell reasons separately.
    counts = Counter(f"{row['strategy']} {row['side']}" for row in weekly)

    reasons = Counter(row['reason'] for row in weekly if row['side'] == 'SELL')

    # Assemble report values, holdings, activity totals, observed drawdown, and data-quality notes.
    return dict(start = start.isoformat(), 
                end = end.isoformat(), 
                timezone = settings['timezone'],
                opening_value = opening['portfolio_value'] if opening else None,
                closing_value = closing['portfolio_value'] if closing else None,
                closing_quote = closing['timestamp'] if closing else None,
                closing_age_seconds = closing_age,
                cash = closing['cash'] if closing else None,
                dca_btc = closing['dca_btc'] if closing else None,
                swing_btc = closing['active_btc'] if closing else None,
                change_usd = change, return_pct = return_pct,
                realized_pnl = sum(row['realized_pnl'] or 0 for row in weekly),
                unrealized_pnl = unrealized,
                fees = sum(row['fee_usd'] for row in weekly), trade_count = len(weekly),
                counts = dict(counts), exit_reasons = dict(reasons),
                observed_drawdown_pct = drawdown if len(observed) >= 2 else None,
                quote_count = len(snapshots), largest_quote_gap_seconds = largest_gap,
                warnings = warnings, has_period_activity = bool(snapshots or weekly))


# Render the computed report as plain text suitable for an email body.
def render_report(report, title='Weekly paper-trading report'):

    # Format monetary values to two decimal places and show unavailable values as N/A.
    def money(v): 
        return 'N/A' if v is None else f'${v:,.2f}'
    
    # Format numeric metrics to the requested precision and show unavailable values as N/A.
    def number(v, digits=2): 
        return 'N/A' if v is None else f'{v:,.{digits}f}'
    
    # Convert reporting boundaries to the configured local timezone for display.
    zone = ZoneInfo(report['timezone'])
    start, end = stamp(report['start']).astimezone(zone), stamp(report['end']).astimezone(zone)

    # Build the report heading, valuation summary, holdings, profit figures, and fee totals.
    lines = [title, 'PAPER TRADING — simulated trades; no real funds',
             f'Period: {start:%Y-%m-%d %H:%M %Z} to {end:%Y-%m-%d %H:%M %Z} (end excluded)', '',
             f"Opening portfolio value: {money(report['opening_value'])}",
             f"Closing portfolio value: {money(report['closing_value'])}",
             f"Actual closing quote timestamp: {report['closing_quote'] or 'N/A'}",
             f"Quote age at period end (seconds): {number(report['closing_age_seconds'])}",
             f"Portfolio change: {money(report['change_usd'])}",
             f"Portfolio return (%): {number(report['return_pct'])}",
             f"Observed maximum drawdown (%): {number(report['observed_drawdown_pct'])}", '',
             f"Cash at closing quote: {money(report['cash'])}",
             f"DCA BTC: {number(report['dca_btc'], 8)}",
             f"Swing BTC: {number(report['swing_btc'], 8)}",
             f"Realized P/L on sales during this period: {money(report['realized_pnl'])}",
             f"Unrealized P/L on ALL holdings at closing quote: {money(report['unrealized_pnl'])}",
             f"Fees during period: {money(report['fees'])}",
             f"Fills during period: {report['trade_count']}"]
    
    # Append sorted fill counts for each strategy and trade side.
    lines += [f'  {key}: {value}' for key, value in sorted(report['counts'].items())]

    # Append sorted exit-reason counts beneath their own label.
    lines += ['', 'Exit reasons:'] + [f'  {key}: {value}' for key, value in sorted(report['exit_reasons'].items())]
    
    # State explicitly when no sales were recorded in the period.
    if not report['exit_reasons']: 
        lines.append('  No sales recorded.')

    # Append quote coverage statistics and any data-quality warnings.
    lines += ['', f"Recorded quotes in period: {report['quote_count']}",
              f"Largest quote gap (seconds): {report['largest_quote_gap_seconds']:.0f}"]
    
    lines += ['', 'Data notes:'] + (report['warnings'] or ['No coverage warning at the configured thresholds.'])
    
    # Explain the limits of sampled drawdown and the distinction between profit measures and weekly change.
    lines += ['', 'Drawdown is sampled from saved quotes, not every market tick.',
              'Realized P/L includes fees in the recorded cost basis/proceeds.',
              'Realized P/L plus ending unrealized P/L is not the weekly portfolio change;',
              'weekly change also depends on unrealized P/L at the start of the week.',
              'Value changes assume no deposits or withdrawals. No LLM commentary is included yet.']
    
    # Join all report lines into a newline-terminated plain-text message.
    return '\n'.join(lines)+'\n'
