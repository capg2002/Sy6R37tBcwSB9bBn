# Bitcoin Trading Agent — Seven-Day Evaluation Log

**Evaluation type:** Paper trading using real Coinbase market data; simulated funds and executions.

**Period:** October 14, 2026 at 16:29:26.157744 EDT to October 21, 2026 at 16:29:26.157744 EDT, end excluded. This is exactly 168 hours. Each “day” below is a consecutive 24-hour interval beginning at 16:29:26 EDT, rather than a midnight-to-midnight calendar day.

**Evidence:** Uploaded `paper.sqlite`, uploaded `paper.log`, and the supplied weekly Gmail report. This document was reconstructed retrospectively from saved records; it is not a contemporaneous handwritten diary. The interval starts at the database’s first recorded quote and ends exactly seven days later, rather than being selected around a profitable closing observation. Records after that endpoint are excluded from the evaluation.

## Evaluation objective

The evaluation examined whether the system could receive live market data, make hourly trading decisions, simulate DCA and swing trades, calculate GARCH forecasts, manage a trailing stop, retain account state, and record operational problems over seven elapsed days. Continuous availability and trading profitability are evaluated separately: the records show a week-long experiment with interruptions, not seven uninterrupted days of monitoring.

## Results

| Measure | Recorded result |
| --- | --- |
| Opening observed account value | $10,000.00 |
| Closing observed account value | $10,017.99 |
| Observed account change | +$17.99 (+0.1799%) |
| Closing quote timestamp | 2026-10-21 16:29:22 EDT |
| Closing quote age at endpoint | 4.11 seconds |
| Sampled maximum drawdown | 0.1317% |
| Quote snapshots | 30,158 |
| Completed-close decisions | 86 |
| Recorded GARCH forecasts | 86; all recorded status values are ok |
| Invalid-or-late candle events | 61 |
| Fills | 4: two DCA buys, one swing buy, one swing sell |
| Trading fees | $0.75 |
| Realized swing P/L | -$0.69 |
| Ending unrealized DCA P/L | $18.68 |
| Ending cash | $9,449.31 |
| Ending holdings | 0.00673041 DCA BTC; no swing position |
| Execution uncertainty | Flagged during the evaluation |
| Longest consecutive-quote gap | 6 hours 56 minutes 37.96 seconds |

The closing value uses the last quote inside the period, about four seconds before the endpoint. The gain mainly reflects unrealized appreciation of the DCA holdings; the completed swing trade lost money. Fees are already included in account balances and P/L and are not subtracted again.

Because monitoring gaps occurred and execution uncertainty was flagged, these figures describe the saved paper-account continuation. They do not verify the execution path during unobserved intervals or establish profitable trading performance. The sampled drawdown can miss larger movements between observations.

## Configuration and the recorded change

Initial simulated capital was $10,000, allocated as $6,000 DCA cash, $3,000 swing cash, and $1,000 reserve. Strategy mode remained `hybrid`; swing entry used a bearish SMA crossover. Fees were 0.1%, configured slippage was zero, ATR used 14 periods and a multiplier of two, and GARCH trailing stops used a multiplier of three. Bearish-crossover exits remained enabled.

The database records one Google Sheets configuration update on **October 16 at 14:48:43 EDT**:

| Setting | Before update | After update |
| --- | --- | --- |
| DCA purchase budget | $500 | $50 |
| DCA drop threshold | 3% | 0.1% |
| Base swing purchase budget | $1,000 | $100 |
| GARCH purchase sizing | Enabled | Disabled |

GARCH trailing stops stayed enabled. Forecast settings remained a 4,320-hour window, 500 minimum observed adjacent returns, five conditional paths, and hourly forecasting.

This is an evaluation of an evolving configuration, not a controlled comparison of two fixed strategies. The only swing purchase occurred after GARCH sizing was disabled. Its recorded sizing decision approved $100 with multiplier 1.0000 and a valid volatility forecast. Consequently, this run demonstrates GARCH forecasting and trailing-stop management, but does not demonstrate a purchase reduced by volatility-based sizing. The reason for the operator's settings change is not recorded.

## Daily activity log

“Last value” is the final observed quote valuation within each interval. “Skipped” counts explicit `invalid_or_late_candle` events, which do not distinguish the two causes. These counts do not include every hour missed during a long interruption. Warning counts are log lines, not unique outages.

| Day / dates, 16:29 to 16:29 | Quotes | Close decisions | Skipped | Fills | Last value | Warnings |
| --- | --- | --- | --- | --- | --- | --- |
| 1: Oct 14–15 | 4,748 | 12 | 11 | 1 | $10,003.03 | 36 |
| 2: Oct 15–16 | 6,709 | 19 | 5 | 1 | $9,998.71 | 21 |
| 3: Oct 16–17 | 7,162 | 21 | 2 | 1 | $9,998.67 | 29 |
| 4: Oct 17–18 | 1,906 | 7 | 9 | 1 | $9,992.56 * | 25 |
| 5: Oct 18–19 | 4,057 | 11 | 10 | 0 | $10,000.53 | 30 |
| 6: Oct 19–20 | 2,374 | 6 | 13 | 0 | $10,005.02 | 46 |
| 7: Oct 20–21 | 3,202 | 10 | 11 | 0 | $10,017.99 | 34 |

*Day 4’s final valuation is 6 minutes 53.39 seconds before its endpoint, exceeding the reporter's five-minute freshness threshold. It is shown as the last observed value, not a fresh endpoint valuation. The other six daily closing ages are approximately 3–11 seconds. Full timestamps and boundary ages are provided in `daily_activity.csv`.

### Day 1 — October 14–15

The first saved quote valued the account at $10,000. The log recorded historical warm-up and waiting for a new hourly candle. Early Sheets refreshes failed with `ModuleNotFoundError` and `ValueError`; messages state that saved settings were retained. Three orderly shutdown messages appear before the first trade. At 21:00:53 on October 14, the system bought 0.00611416 BTC for a $500 fee-inclusive DCA budget. Twelve close decisions were recorded. The longest within-day observation gap was approximately 107.92 minutes.

**Finding:** Live ingestion, initial DCA execution, and configuration fallback produced records. Startup and data-access interruptions prevent a claim of continuous operation.

### Day 2 — October 15–16

Nineteen close decisions were recorded. An orderly shutdown occurred at 14:48:36 on October 16, followed by the recorded Sheets configuration update at 14:48:43. The next DCA fill, at 15:00:47, spent the revised $50 budget and acquired 0.00061626 BTC. The final observed account value was $9,998.71. The longest within-day observation gap was approximately 17.87 minutes.

**Finding:** The audit history records a configuration change followed by a trade using the changed budget. A change during testing limits before/after performance comparisons.

### Day 3 — October 16–17

At 00:00:43 on October 17, a bearish crossover produced a $100 swing purchase. Twenty-one close decisions were recorded. Five GARCH stop updates raised the initial stop from $80,590.09 to $81,038.87. The first quote snapshot carrying `execution_path_uncertain = true` is at 10:01:23 on October 17. The swing position remained open at this day's endpoint.

**Finding:** The records connect a crossover entry, an accepted forecast, and successive stop adjustments. The uncertainty flag means monitoring coverage was insufficient to verify the complete position path.

### Day 4 — October 17–18

At 17:31:30 on October 17, the entire swing position sold at $81,032.77 with exit reason `garch_trailing_stop`, realizing a $0.69 loss after fees. Later, quotes were absent between October 17 at 19:10:36 and October 18 at 02:07:14: the largest consecutive-quote gap in this evaluation. Seven close decisions were recorded. The last observed valuation was $9,992.56 and was stale at the daily endpoint.

**Finding:** The trailing-stop exit worked in the recorded simulation, but a trailing-stop exit does not necessarily mean a profitable trade. The longest quote gap occurred after the swing position had closed; DCA holdings remained exposed.

### Day 5 — October 18–19

No fills were recorded. Eleven close decisions and ten explicit invalid-or-late candle events were recorded. The final observed value was $10,000.53. The supplied weekly Gmail message was delivered during this interval, but its reporting window ended at midnight October 19 and differs from this evaluation's window.

**Finding:** Reporting provides a separate summary of saved activity. Changes in account value without trades reflect valuation of existing BTC holdings. The available records do not assign an exhaustive reason to every no-trade hour.

### Day 6 — October 19–20

No fills were recorded. Six close decisions and thirteen invalid-or-late candle events were recorded. The log contains 46 warning lines. The final observed value was $10,005.02, and the longest within-day observation gap was approximately 100.99 minutes.

**Finding:** Recorded quote collection and decisions continued, with substantial interruptions. Warning counts and missing observations should be investigated before describing the service as reliable 24/7 operation.

### Day 7 — October 20–21

No fills were recorded. Ten close decisions and eleven invalid-or-late candle events were recorded. The account finished the evaluation with DCA holdings of 0.00673041 BTC, no swing position, and a final observed value of $10,017.99. The longest within-day observation gap was approximately 88.11 minutes.

**Finding:** The system retained its holdings and continued recording valuations through the final day. The final gain came from the retained DCA exposure rather than additional trading activity.

## Trade ledger

All times below are EDT. Buy budgets include fees; sale proceeds are net of fees. BTC quantities are displayed to eight decimal places, with full precision retained in `trades.csv`.

| Time | Action | BTC | Fill price | Budget / proceeds | Reason |
| --- | --- | --- | --- | --- | --- |
| 2026-10-14 21:00:53 EDT | DCA BUY | 0.00611416 | $81,695.75 | $500.00 | dca_drop_threshold |
| 2026-10-16 15:00:47 EDT | DCA BUY | 0.00061626 | $81,054.14 | $50.00 | dca_drop_threshold |
| 2026-10-17 00:00:43 EDT | SWING BUY | 0.00122677 | $81,433.32 | $100.00 | bearish_crossover |
| 2026-10-17 17:31:30 EDT | SWING SELL | 0.00122677 | $81,032.77 | $99.31 | garch_trailing_stop |

All four database trade IDs appear in the application log. Matching trade timestamps demonstrate that the log's timezone is consistent with EDT, four hours behind the database's UTC timestamps.

## GARCH and stop-management evidence

All 86 committed volatility events in the selected interval have status `ok` and report five fitted paths. Recorded hourly sigma values range from 0.2141% to 1.0245%. This describes stored successful forecasts; it does not imply that a forecast was completed for all 168 hours or that forecasts were statistically accurate. No predictive-accuracy benchmark was computed.

The swing entry's sigma was approximately 0.2745% per hour. The following recorded stop updates demonstrate the trailing mechanism. Times are the event payload's decision times; live application can occur later after computation completes.

| Decision time | Previous stop | Updated stop | Hourly sigma |
| --- | --- | --- | --- |
| 2026-10-17 01:00:00 EDT | $80,590.09 | $80,780.88 | 0.2612% |
| 2026-10-17 02:00:00 EDT | $80,780.88 | $80,844.66 | 0.2503% |
| 2026-10-17 03:00:00 EDT | $80,844.66 | $80,924.47 | 0.2418% |
| 2026-10-17 04:00:00 EDT | $80,924.47 | $81,034.68 | 0.2401% |
| 2026-10-17 05:00:00 EDT | $81,034.68 | $81,038.87 | 0.2319% |

The final stop was still below the $81,433.32 entry price. The subsequent $81,032.77 sale was below the stop, consistent with execution at an observed bid rather than a guaranteed stop-price fill. GARCH sizing was disabled for this entry, so the demonstrated use of volatility was stop adjustment rather than reducing the purchase budget.

## Reliability and error handling

During the selected interval, the log contains 221 warning lines: 193 market-data request failures, 22 history-unavailable warnings, three rejected/unavailable Sheets refreshes, and three Google authentication DNS retry warnings. The Sheets failures were classified as `ModuleNotFoundError`, `ValueError`, and `TransportError`. Four explicit orderly shutdown messages were recorded.

Database runtime events include 190 quote-gap recoveries, 61 invalid-or-late candle events, and 86 completed-close decisions. These categories come from different detection paths and should not be added together as a count of outages. The 221 warnings likewise are not 221 independent outages.

The log reports retaining saved state on failed market requests and retaining saved configuration after rejected Sheets updates. Records continue after interruptions. No `ERROR`-level lines were found in the selected interval, but warning-level failures and missing observations are meaningful operational limitations.

| Gap begins | Next recorded quote | Elapsed gap |
| --- | --- | --- |
| 2026-10-17 19:10:36 EDT | 2026-10-18 02:07:14 EDT | 6.94 hours |
| 2026-10-14 16:59:52 EDT | 2026-10-14 18:47:46 EDT | 1.80 hours |
| 2026-10-19 07:24:37 EDT | 2026-10-19 09:12:17 EDT | 1.79 hours |
| 2026-10-19 19:56:36 EDT | 2026-10-19 21:37:36 EDT | 1.68 hours |
| 2026-10-20 07:42:34 EDT | 2026-10-20 09:18:17 EDT | 1.60 hours |

Gap duration measures time between observations, including ordinary polling time. It does not establish the cause or prove the process was stopped for that entire duration. Some warnings explicitly identify network failures; the records alone cannot attribute every gap to sleep, connectivity, manual shutdown, or another cause. Quote counts are not converted into an uptime percentage because polling intervals, retries, and processing times vary.

## Reconciliation with the Gmail report

The supplied email covers October 12 at 00:00 through October 19 at 00:00 EDT. Re-running the repository's weekly-report calculation on the uploaded database reproduces its 22,251 quotes, $10,004.10 closing value, four fills, approximately $0.75 fees, and 232,166-second largest coverage gap.

That 232,166-second gap is from the email's Monday start boundary to the first database quote on Wednesday October 14 at 16:29:26. The reporter includes interval boundaries when calculating coverage gaps. It therefore represents missing coverage before the first saved observation, not proof of a 64.5-hour interruption after this account began recording quotes. This also explains why the email could not calculate an opening valuation or weekly return.

This evaluation instead starts with that first $10,000 observation and runs for exactly seven days. Its closing value and return differ because its endpoint is October 21 in the afternoon. Neither result replaces the other; they summarize different periods.

## Strengths demonstrated and limitations

**Demonstrated:** live quote ingestion; DCA and swing simulated fills; recorded GARCH forecasts; five monotonic trailing-stop increases; a trailing-stop sale; configuration-change auditing; saved-state continuation; and explicit missing-data/uncertainty disclosures. The supplied email also demonstrates report generation and delivery for its separate reporting period.

**Not established:** uninterrupted 24/7 availability, profitable strategy performance, improved results from GARCH sizing, forecast calibration, or successful handling of every possible failure. Only one swing position was completed, settings changed during the run, and observation gaps made execution uncertain. The advisor and report-delivery sidecar databases were not supplied, so Gemini response history and scheduled-delivery deduplication were not evaluated. No live-money exchange orders were submitted.

**Next evaluation:** run the worker on an always-on host, investigate the recorded connectivity/dependency failures, keep one fixed documented configuration for the full test, and evaluate sizing-enabled versus sizing-disabled runs separately. Retain quote, forecast, trade, and configuration records and monitor gap alerts. These are proposed improvements, not changes demonstrated during this run.

## Calculation and evidence notes

- Database access was read-only. SQLite `PRAGMA quick_check` returned `ok`; this verifies database consistency, not whether an omitted WAL or other unsupplied file contains newer records.
- Quote valuations use only `snapshots` rows whose event keys begin `quote:`. Close-decision snapshots are excluded because their timestamps label candle opens rather than live monitoring times.
- The opening value is the first saved quote, which defines the evaluation start. The closing value is the last quote strictly before start + 168 hours. Return is closing/opening − 1; drawdown is the largest fall from a prior observed running peak within this interval.
- Trades are assigned using their fill timestamps. Runtime, forecast, and stop counts are assigned by their linked event commit timestamps. Log warnings are assigned by their timestamp prefixes, interpreted as EDT based on matching trade records.
- Daily gap maxima include each day's start and end boundaries. They can split gaps crossing a daily boundary. The overall longest consecutive-quote gap is calculated directly across the full selected period.
- The day-4 ending valuation is explicitly marked stale. No missing prices, trades, or daily observations were invented. No-trade periods are reported as such without assuming every possible reason.
- The uploaded log continues until October 22 at 09:53 EDT, while database quotes end at 09:47 EDT. Both extend past the selected endpoint, so this difference does not affect the selected seven-day results.
- The precision shown in the human-readable timestamps is seconds; the interval calculation retains the exact microsecond start stated above.

### Source fingerprints

- `paper.sqlite` SHA-256: `fac80f2d090ecff7d72e539e8840214bacde9b94d9ae48ce6167f6bf8bafe904`
- `paper.log` SHA-256: `0580516042b24e81c5feb9a209a6b710b02fe715d756596163623cd0ca4a52e8`
