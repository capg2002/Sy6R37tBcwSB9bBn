# Next milestone: Monday Gmail reports

This package adds a separate reporting process to your existing paper trader.
It reads your paper SQLite database, builds a text report, and optionally sends
it through Gmail. There are NO replacements for your existing trading files.

## What to copy

| New file | Location inside bitcoin-trading-agent |
| --- | --- |
| weekly_reports.py | Project root, beside main.py and paper_loop.py |
| reporting.json | Project root |
| requirements-reporting.txt | Project root |
| services/weekly_report.py | Existing services folder |
| services/gmail_sender.py | Existing services folder |
| services/report_delivery.py | Existing services folder |
| tests/test_weekly_reports.py | Existing tests folder |
| README_GMAIL.md | Root or docs folder |

Copy the individual files into those folders. Keep your existing files.

Implementation contract: this targets the SQLite schema we previously built:
checkpoint.mode, snapshots with event_key/timestamp/value/cash/BTC/payload_json,
and trades with fee_usd/realized_pnl/fill_json. It does not import your trading
engine. If your current schema differs, send the missing table/column error;
do not reset the account to work around it.

## 1. Install and run offline tests

Open a terminal in your project root, with your usual virtual environment active:

```bash
python -m pip install -r requirements-reporting.txt
python -m unittest discover -s tests -p test_weekly_reports.py -v
```

There are 12 offline tests covering calculations, time boundaries and daylight
saving, missing valuations, rejected backtest databases, delivery retries,
restart deduplication, and uncertain email delivery. Tests use temporary databases
and mocked senders; they do not send email.

## 2. Preview a report BEFORE setting up Gmail

```bash
python weekly_reports.py --db runtime/paper.sqlite --preview --current-week
```

Use the actual path of your running PAPER account. The report is printed and saved
as text and JSON under runtime/reports. This command does not send an email.
It does not change the trading database.

The default preview without --current-week covers the most recently completed
calendar week. The current-week option covers the start of this week through now,
which is useful while your project has only a few days of paper data.

If you have only run main.py, start paper_loop.py first: main.py creates backtests,
whereas this report intentionally reads a paper account and its live quote snapshots.

## 3. Gmail authentication: a different account mechanism from Sheets

Your Sheets service account is used to read the private spreadsheet. This sender
uses a Gmail mailbox plus a Gmail app password; the Sheets JSON key is not used.
There is no need to enable the Gmail API for this SMTP implementation.

Use the Gmail account approved for this project (a personal Gmail account can be
used for a personal demo). For an account that permits app passwords:

1. Sign in to https://myaccount.google.com/ using the intended sending account.
2. Open Security and enable 2-Step Verification if it is not already enabled.
3. Open https://myaccount.google.com/apppasswords . You may need to sign in again.
4. Create an app password named `Bitcoin paper reports`.
5. Copy the generated password into your LOCAL .env as shown below. Do not use
   your normal Google account password.

Google may not offer app passwords for managed work/school accounts, Advanced
Protection, or certain security-key-only configurations. If unavailable, use
your employer's approved OAuth/SMTP arrangement; this specific sender needs an
app password and cannot use the Sheets service-account key instead.

Official guidance:
- https://support.google.com/accounts/answer/185833?hl=en
- https://developers.google.com/workspace/gmail/imap/imap-smtp

Append to your existing .env beside weekly_reports.py, keeping Telegram and Sheets
settings intact:

```dotenv
GMAIL_SENDER=your-sending-address@gmail.com
GMAIL_APP_PASSWORD=your_generated_app_password
REPORT_RECIPIENT=your-own-address@example.com
```

For the first test, set REPORT_RECIPIENT to your own real email address. It can be
the same Gmail address as GMAIL_SENDER. One recipient is supported in this version.
Spaces in the app password as displayed by Google are removed by the code.
Never put this password in Google Sheets or commit .env to source control.

## 4. Send a test email yourself

This command DOES send one email to REPORT_RECIPIENT:

```bash
python weekly_reports.py --db runtime/paper.sqlite --send-test
```

It sends a current-week report marked TEST. It does not mark any scheduled weekly
report as sent, so your Monday report remains eligible.

Expected success message:

```text
Test email accepted by Gmail. Check the destination inbox/spam.
```

SMTP acceptance means Gmail accepted the message, not proof of inbox delivery.
If a test send times out with an uncertain outcome, check Sent and the recipient
inbox before running --send-test again. Manual tests are not deduplicated.

## 5. Set the reporting schedule

reporting.json contains:

```json
{
  "timezone": "America/Toronto",
  "weekday": 0,
  "hour": 9,
  "minute": 0,
  "poll_seconds": 30,
  "retry_seconds": 300,
  "max_attempts": 5,
  "max_quote_age_seconds": 300,
  "max_quote_gap_seconds": 300
}
```

- weekday: Monday=0, Tuesday=1, ... Sunday=6.
- hour/minute: local sending time in the specified IANA timezone.
- poll_seconds: check the schedule every 30 seconds; delivery occurs at or shortly
  after the scheduled time, subject to network and process availability.
- retry_seconds/max_attempts: retry known unsuccessful sends after five minutes,
  up to five attempts. Unknown outcomes require reconciliation.
- max_quote_age_seconds: reject weekly returns if a boundary valuation is over
  five minutes old. You still see the old value and its actual timestamp.
- max_quote_gap_seconds: warn when gaps between saved observations exceed five
  minutes. This threshold is a diagnostic policy, not a guarantee of tick coverage.

These report settings are kept in a separate local file for this milestone and
loaded on reporter startup. Restart the REPORTER after changing them. Do not add
these rows to the existing Config sheet or config.json: its validator currently
expects the trading schema. Moving these reporting settings into a separate sheet
with validated hourly refresh is a remaining configuration requirement.

The default Monday 09:00 report covers the previous Monday 00:00 up to this Monday
00:00, excluding the end instant. Trades after midnight Monday belong to the next
week. Changing weekday also changes the calendar-week boundary. Daylight-saving
weeks are correctly allowed to have 167 or 169 hours.

## 6. Run continuously in a THIRD terminal

Keep your trader in terminal 1:

```bash
python paper_loop.py --db runtime/paper.sqlite --sheets --telegram
```

Keep the Telegram notifier in terminal 2:

```bash
python notifications.py --db runtime/paper.sqlite
```

Start the reporter in terminal 3:

```bash
python weekly_reports.py --db runtime/paper.sqlite --watch
```

All three terminals should use the same project environment. The computer must
stay awake and the process must remain running. Docker/cloud deployment comes
later and will keep these services running independently of your laptop.

To check/send a due report once (for a future external scheduler), use:

```bash
python weekly_reports.py --db runtime/paper.sqlite --send-due
```

On restart, the reporter considers the MOST RECENT scheduled week. If that period
has data and has not been recorded, it queues it and attempts delivery immediately.
Thus, first starting on a Friday can send last Monday's due report. A completed
week with no paper quotes or fills is skipped. Previously queued unsent reports
are retained for retries; missing older unqueued weeks are not automatically
backfilled after a multi-week outage.

## What the report means

- Portfolio change/return: difference between saved quote valuations near the two
  week boundaries. N/A if either boundary is missing/stale. A partial first week
  commonly has no valid opening valuation; that is expected.
- Cash and DCA/swing BTC: holdings at the actual closing quote timestamp.
- Realized P/L: net realized P/L for sales DURING the week. This can include gains
  on positions opened in a previous week.
- Unrealized P/L: gain/loss on ALL holdings still open at the closing quote,
  reconstructed using the full recorded fill history and fee-inclusive cost basis.
- Fees and fills: sums/counts DURING the week, split by strategy and buy/sell.
- Exit reasons: counts from the recorded sale reasons.
- Maximum drawdown: sampled from saved QUOTE snapshots. It cannot reveal market
  movement while the program was offline or between quotes.
- Missing quotes and execution_path_uncertain: explicitly disclosed.

Do not add weekly realized P/L to ending unrealized P/L and call it weekly profit:
the portfolio also had unrealized gains/losses at the beginning of the week.
Fees are already reflected in fills/accounting and are not subtracted again.
These calculations assume your current no-deposits/no-withdrawals account model
and an intact history of fills. They value BTC at the saved quote midpoint and do
not deduct a hypothetical fee for selling all remaining BTC.

The reporter deliberately ignores close-decision snapshots: the existing engine
labels them by the candle's OPEN timestamp even though the decision is made after
its close. Live quote snapshots have the actual monitoring time.

## Delivery tracking and duplicate prevention

The reporter creates a SEPARATE SQLite database alongside your paper account:

```text
runtime/paper.sqlite.reports.sqlite
```

It stores report periods, frozen email bodies/recipients, attempts, status, and
Message-ID. It never writes the trading database or changes its revision.
Keep this file when restarting/moving/deploying the account. Do not point multiple
reporters at copies of it or delete it to fix an error: that loses deduplication.
Its identity is bound to the original database path; migrating paths needs a
planned migration rather than accidentally making a new delivery history.

The same report period will not normally send twice, even if the process restarts
or you edit the recipient. Once queued, a report retains its original body and
recipient. Recipient edits apply to future reports.

Inspect status (also works while --watch runs):

```bash
python weekly_reports.py --db runtime/paper.sqlite --status
```

Statuses: pending, sending, sent, uncertain, failed.

SMTP does NOT provide an exactly-once delivery transaction with SQLite. If Gmail
accepts a message and the process crashes before recording success, the ledger
cannot know what happened. It marks the attempt uncertain on restart and does
not automatically resend. Search Gmail's Sent folder for the report ID shown in
--status (also in its subject/body).

To resolve an uncertain result, first stop the reporter with Ctrl+C:

```bash
python weekly_reports.py --db runtime/paper.sqlite --resolve REPORT_ID --resolution sent
```

Or, only after checking and deciding another attempt is appropriate:

```bash
python weekly_reports.py --db runtime/paper.sqlite --resolve REPORT_ID --resolution retry
python weekly_reports.py --db runtime/paper.sqlite --send-due
```

Replace REPORT_ID with the actual weekly-... identifier. Manually authorizing a
retry after an ambiguous outcome can produce a duplicate. The same retry command
can re-enable a failed report after you fix credentials or connectivity.

## What this milestone does not add

There is no LLM-generated strategy commentary yet. The report reports actual
stored activity without inventing explanations or recommendations. It also does
not add new trading strategies, change GARCH settings, or tune profitability.

After a preview and test email work, run the Monday schedule. The next development
step is a bounded LLM advisory interface with explicit allowed actions and logged
fallback behavior, followed by cloud deployment and the week-long evaluation.
