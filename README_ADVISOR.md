# Advisory-only LLM integration

This package is based on your latest uploads: paper_loop(1).py, engine(3).py,
config(9).json, storage.py, config_validation(1).py and sheet_config.py.
It preserves the currently configured trading mode and strategy parameters.

## What to copy

Stop paper_loop.py with Ctrl+C before replacing its launcher. Keep a backup.

| File | Action |
| --- | --- |
| paper_loop.py | Replace the root paper_loop.py |
| advisor.py | Add beside main.py |
| advisor.json | Add at the project root |
| requirements-advisor.txt | Add at the project root |
| services/advisor_context.py | Add inside existing services |
| services/llm_advisor.py | Add inside existing services |
| services/advisor_log.py | Add inside existing services |
| tests/test_advisor.py | Add inside existing tests |
| README_ADVISOR.md | Keep at root or in docs |
| paper_loop_changes.patch | Optional review of the launcher changes |

Merge individual files into your folders; do not replace whole folders.
Do not replace your engine, strategies, storage, config validation, sheet reader,
or config.json. No new Google Sheet rows or configuration migration are required.
Your existing strategy_mode migration still needs to have been completed.

## A bug found in the uploaded launcher

Your monitor_quote() called process_quote() INSIDE `if gap:`. On an ordinary
quote, including the first one, `result` was undefined when returned. The supplied
launcher moves that call outside the conditional. Gap recovery still cancels
pending buys; EVERY valid quote now reaches the existing execution logic.
This is the only execution-related fix. The other edits only capture advisory
input alongside the existing close-decision event. The patch shows all changes.

## How it works

1. The paper runner prepares market indicators from the same frame used for the
   GARCH forecast. No additional forecasting or API call is added to that process.
2. When the forecast finishes and the completed-candle decision is accepted, the
   runner saves a compact advisor_input object in the runtime close event, in the
   SAME transaction as that decision. The input is never exposed before commit.
3. advisor.py, running independently, reads the latest committed event using a
   READ-ONLY connection to the paper database.
4. It reserves one job in paper.sqlite.advisor.sqlite, checks input age/data/call
   limits, and calls the OpenAI Responses API with a strict JSON schema.
5. It records the result, explanation, limitations, model, prompt/version,
   settings, inputs, token usage (when returned), latency, and error status.
6. Nothing reads that recommendation back into the trading engine. It cannot
   change strategy_mode, balances, pending orders, stops, or Google Sheets.

The prompt has no tools, browsing, order execution, filesystem, or database-write
functions. Its only output is a recommendation for hybrid/dca_only/pause_entries.
A paused-entry recommendation is not a sell signal. Protective exits remain part
of your deterministic engine in every mode.

## 1. Install and run the offline tests

From the project root, with your usual virtual environment active:

```bash
python -m pip install -r requirements-advisor.txt
python -m unittest discover -s tests -p test_advisor.py -v
```

The project already needs pandas; the tests use it. There are 11 offline tests.
They send no network requests and use temporary databases/mocked providers.

## 2. Restart your trader and wait for a new close decision

Use the same account path as before:

```bash
python paper_loop.py --db runtime/paper.sqlite --sheets --telegram
```

Leave your Telegram notifier and Gmail reporter running in their usual terminals.
The advisor is a FOURTH independent process when all services are running.

On first startup the trader warms up and waits for the next completed hourly
candle. Old close events do not contain advisor_input. Wait for a NEW
`Close processed: ... (close_decision)` log entry from this updated runner.
GARCH may take some additional time. A skipped/late candle does not create advice.

Preview the exact numerical input without calling OpenAI:

```bash
python advisor.py --db runtime/paper.sqlite --input
```

If you see `No advisory input yet`, wait for the next valid close decision. Do not
reset your database. This command never reads credentials or sends any data.

## 3. Configure API access

The OpenAI API uses an OPENAI_API_KEY. Your Google service-account JSON and Gmail
app password cannot authenticate to OpenAI. Create a project API key through
https://platform.openai.com/api-keys and ensure that project has API billing/access
available. Check API billing independently of your ChatGPT subscription.

Add to your EXISTING .env beside advisor.py:

```dotenv
OPENAI_API_KEY=your_actual_openai_api_key
```

Keep all your Telegram, Google Sheets and Gmail entries. Do not put the key into
a sheet, code file, screenshot, or repository. Keep .env ignored by git.

advisor.json selects the model and runtime limits:

```json
{
  "model": "gpt-4.1-mini",
  "poll_seconds": 10,
  "timeout_seconds": 30,
  "max_output_tokens": 500,
  "max_input_age_seconds": 900,
  "max_requests_per_utc_day": 24
}
```

The default is a configurable model supporting structured outputs. If your API
project cannot access it, select an available Responses/structured-output model
and check its parameter requirements. SDK retries are disabled. The timeout is
an SDK/network timeout, not a hard real-time deadline; a slow API never blocks
trading because the processes are separate. Results arriving after the input-age
limit are marked late and remain advisory only.

These advisor settings are local and loaded on worker startup for this milestone;
restart the advisor to change them. They are not yet managed through Google Sheets.
Do not add these keys to the trading Config tab, whose schema is unchanged.

## 4. Make one real API request

Once --input shows a fresh saved close:

```bash
python advisor.py --db runtime/paper.sqlite --once
python advisor.py --db runtime/paper.sqlite --show
```

--once may incur API charges. It processes at most the latest input; it does not
replay historical data. Calling it again for the same candle does not make
another request, even if the first request failed or the model was changed.

--show displays up to ten recorded entries without contacting OpenAI. Example
SHAPE only (not a prediction or a tested trading recommendation):

```json
{
  "recommended_mode": "dca_only",
  "reason": "Forecast volatility is elevated relative to the supplied reference.",
  "limitations": ["Volatility does not establish future price direction."]
}
```

If this differs from your current mode, the trader stays in its CURRENT mode.
You do not have to approve or act on each recommendation.

## 5. Run continuously

```bash
python advisor.py --db runtime/paper.sqlite --watch
```

The worker polls for new committed closes every 10 seconds, not for a new API
request every 10 seconds. The normal rate is at most one attempt per hourly
candle, additionally capped at 24 attempts per UTC day for this ledger.
A failed request counts toward the cap because its billing outcome can be unknown.
Input and output sizes are bounded, but the cap is not a dollar spend guarantee.
Use your OpenAI project's usage/budget controls as well.

Ctrl+C stops only the advisor. Your trading, Telegram and Gmail processes continue.
Keep these files with the account when backing up/deploying:

- runtime/paper.sqlite: original trading state, now including advisory inputs.
- runtime/paper.sqlite.advisor.sqlite: requests and recommendation history.
- runtime/paper.sqlite.advisor.log: operational advisor log.

The advisor DB is bound to the source path. Keep one worker and one advisor DB per
account; copying/deleting it or recreating the account at the same path requires
a deliberate migration. Losing the advisor DB also loses request deduplication
and its local daily quota history.

## Interpreting statuses

| Status | Meaning |
| --- | --- |
| ok | Valid recommendation recorded; NOT applied |
| already_recorded | This candle already has a job; no repeat API request |
| waiting_for_new_close | No compatible new close event is available |
| skipped_stale | Candle ended more than 15 minutes ago, or its time is in the future |
| skipped_data | Core price/ATR/SMA inputs are missing or invalid |
| skipped_daily_limit | Configured local daily attempt cap reached |
| refused | Provider declined to produce the structured recommendation |
| incomplete | Provider response did not complete, e.g. output limit |
| invalid_response | Returned data failed parsing or local validation |
| late | Response arrived after the input-age limit; record only |
| error | API/validation failure; exception class recorded without secrets |
| interrupted | Worker stopped after reserving a job but before saving its result |

No request is automatically retried. After fixing an API problem, wait for the
next fresh candle. The worker deliberately skips old candles after an outage.
It is acceptable for advisory coverage to have gaps; those gaps must be reported
in evaluation rather than presented as a complete history.

## What the model sees and what it does not

Inputs include completed-close price, ATR, current/previous SMA20 and SMA50,
1h/24h simple returns when those windows are fully observed, GARCH one-hour sigma
in FRACTION units (0.01 is about 1%), current mode, relevant rule flags, cash pools,
BTC holdings, stop and pending-order flags, data quality and configuration hash.
No private keys, email addresses, account IDs, raw trade history, or full config
are sent. Portfolio amounts ARE sent to OpenAI when you run --once/--watch.
The API request uses store=False; this is not a blanket promise of zero provider
retention. Review your organization's data-handling requirements before deployment.

The portfolio is captured at decision time, after any intervening quote handling
while the GARCH worker ran. Candle-close and last-quote times are explicitly
labelled; these are not falsely presented as a perfectly simultaneous snapshot.

The model has no news feed or on-chain data. Its statements remain fallible even
when JSON structure validates. The prompt explains that GARCH is a volatility
forecast, DCA still increases exposure, and a bearish entry is not a short sale.

## Evaluation before automatic control

Collect recommendations for a fixed period. Check mode-change frequency,
unsupported factual claims, data-quality handling, latency, usage, and failures.
Agreement with your rules alone does not demonstrate value. Later compare a
separate advisory-driven simulation with the same baseline inputs, costs and
information timing, including API latency and failed/skipped recommendations.
Do not use this live API on old backtest dates and call that a leak-free historical
LLM evaluation: model training may include knowledge of later events.

Automatic control, manual override precedence, cooldowns and effect evaluation
are a future milestone. This package does not implement them.

Official API references used:
- https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses
- https://developers.openai.com/api/docs/models/gpt-4.1-mini
