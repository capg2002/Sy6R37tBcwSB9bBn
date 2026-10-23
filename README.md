# Bitcoin Trading Agent

A Python project for historical BTC backtesting and persistent paper trading with live market data. It combines dip-based DCA accumulation, moving-average swing entries, ATR stops, and GARCH-based position sizing and trailing stops. Optional workers provide Telegram notifications, scheduled Gmail reports, Google Sheets configuration, and Gemini strategy-mode recommendations.

**All executions are simulated. The project does not submit real exchange orders.** Gemini recommendations are advisory only and cannot change settings or place trades.

## Features

- Historical hourly BTC-USD data from Yahoo Finance through `yfinance`.
- Live Coinbase public quotes and completed hourly candles for paper trading; no Coinbase trading credentials are required.
- Separate DCA, swing, and reserve allocations with simulated fees and slippage.
- SMA 20/50 crossover entries, ATR stops, optional bearish-crossover exits, and GARCH trailing stops.
- One-hour GARCH(1,1) volatility estimates with Student-t innovations. Volatility estimates measure uncertainty, not price direction.
- SQLite checkpoints, fill history, event deduplication, configuration history, and process locks.
- Explicit missing-data handling and execution-uncertainty flags after monitoring gaps.
- Optional integrations that run independently of the trading process.

## Installation

The included Dockerfile targets **Python 3.14**. Use a fresh virtual environment for native installation; bundled or previously copied environments should not be reused across machines.

```bash
git clone https://github.com/capg2002/bitcoin-trading-agent.git
cd bitcoin-trading-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
```

On Windows PowerShell, create and activate the environment with:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Run commands from the repository root with the environment active. `requirements.txt` covers the maintained application and optional integrations. Scientific dependencies remain unpinned, consistent with the existing service requirement files; this is not a reproducible lockfile. After validating an environment, record its resolved versions with `python -m pip freeze > requirements-lock.txt`.

## Start a paper account

Review `config.json` before the first run, then start the runner:

```bash
python paper_loop.py --db runtime/paper.sqlite
```

The runner initializes a simulated account, saves its state in SQLite, monitors quotes, and processes newly completed hourly candles. Background workers fetch history and calculate volatility. The first available historical close is treated as warm-up rather than replaying old entries.

Stop with **Ctrl+C**. Restart with the same command and database to resume the saved account. Keep the computer awake for continuous monitoring.

For one quote-processing attempt:

```bash
python paper_loop.py --db runtime/paper.sqlite --once
```

`--once` is not a full hourly strategy test: it exits after the quote attempt, before the continuous history/forecast workflow.

Inspect the checkpoint and recent fills:

```bash
python inspect_db.py runtime/paper.sqlite
```

## Trading configuration

The checked-in `config.json` uses these starting settings:

| Setting | Default | Purpose |
| --- | --- | --- |
| `starting_budget` | `10000` | Initial simulated USD capital |
| `dca.allocation` / `swing.allocation` / `reserve` | `6000` / `3000` / `1000` | Capital allocation |
| `dca.amount` / `dca.drop_threshold` | `500` / `0.03` | DCA purchase amount and decline from the last DCA buy |
| `swing.trade_amount` / `swing.crossover` | `1000` / `bear` | Base swing amount and entry crossover direction |
| `atr.period` / `atr.multiplier` | `14` / `2.0` | ATR stop parameters |
| `execution.fee_rate` / `execution.slippage_rate` | `0.001` / `0.0` | Simulated execution costs |
| `garch.window_hours` / `garch.min_observed_returns` | `4320` / `500` | Forecast history and warm-up requirement |
| `garch.forecast_every_hours` | `1` | Required hourly forecast cadence |
| `garch.n_paths` / `garch.seed` | `5` / `123` | Conditional paths for missing-data modelling |
| `risk.reference_hourly_sigma` / `risk.minimum_swing_amount` | `0.004` / `25.0` | Volatility sizing reference and minimum swing amount |

GARCH sizing, GARCH trailing stops, and bearish-crossover exits are enabled in the checked-in configuration. The trailing multiplier is `3.0`.

Supported `strategy_mode` values:

| Mode | New entries permitted |
| --- | --- |
| `hybrid` | DCA and swing |
| `dca_only` | DCA only |
| `pause_entries` | None |

Mode changes restrict new entries and cancel disallowed pending buys; they do not liquidate existing holdings or disable existing-position management. DCA here is **dip-triggered accumulation**, not a fixed calendar purchase schedule. The configured `bear` entry selects a bearish SMA crossover; it does not open a short position.

Existing accounts validate configuration against saved state. Changing initial capital or other locked settings is not a supported way to reset an account. Use a separate database path for an intentionally new simulation.

## Historical backtest

```bash
python main.py
```

This requests one year of hourly BTC-USD data, runs the shared trading engine, and creates a timestamped folder under `data/`. Outputs include raw and prepared market data, configuration and metadata, `trading.sqlite`, trades, portfolio history, volatility history, stop history, risk events, and `summary.json`.

Signals use information available through the current completed candle; pending entries are simulated at a subsequent candle open. Backtest intrabar stop assumptions differ from live quote monitoring. A full hourly GARCH backtest can take substantial time.

Missing OHLC data remains invalid for execution. Conditional log-price paths are used for volatility modelling only. Gaps can make execution paths uncertain, and drawdown is measured from sampled observations. Backtest results do not establish future profitability.

## Optional integrations

Create a local `.env` file in the repository root with only the credentials needed for enabled services:

```dotenv
TELEGRAM_BOT_TOKEN=replace_with_bot_token
TELEGRAM_CHAT_ID=replace_with_chat_id

GOOGLE_APPLICATION_CREDENTIALS=secrets/google-sheets-service-account.json
GOOGLE_SHEETS_ID=replace_with_spreadsheet_id
GOOGLE_SHEETS_RANGE=Config!A:B

GMAIL_SENDER=sender@gmail.com
GMAIL_APP_PASSWORD=replace_with_app_password
REPORT_RECIPIENT=recipient@example.com

GEMINI_API_KEY=replace_with_api_key
```

Keep `.env`, service-account keys, and account databases out of version control. Ignore rules do not remove previously tracked files or credentials from Git history.

### Telegram notifications

Discover chat IDs after sending a message to the bot, then explicitly send a test:

```bash
python telegram_setup.py
python telegram_setup.py --test
```

Enable queuing on the trader and run delivery in another terminal:

```bash
python paper_loop.py --db runtime/paper.sqlite --telegram
```

```bash
python notifications.py --db runtime/paper.sqlite
```

`--telegram` queues notifications for new paper fills; the separate notifier sends them. Do not start a second trader for an account that is already running; restart the existing trader with the desired flags.

### Google Sheets configuration

Enable the Google Sheets API for a service account, save its JSON key at the configured path, and share the spreadsheet with its service-account email. The reader uses a two-column `key` / `value` sheet with dotted configuration keys.

Generate the complete template, then validate remote access:

```bash
python sheets_setup.py --template
python sheets_setup.py
python paper_loop.py --db runtime/paper.sqlite --sheets --telegram
```

The runner polls the sheet hourly. Updates must contain the complete valid schema, and only the live-edit allowlist in `services/sheet_config.py` may change. Accepted settings are persisted with the account and exported to a local fallback cache. Reporting and advisor settings remain in their separate JSON files.

### Weekly Gmail reports

Preview without sending email:

```bash
python weekly_reports.py --db runtime/paper.sqlite --preview --current-week
```

Explicitly send a test, start the scheduler, or inspect delivery state:

```bash
python weekly_reports.py --db runtime/paper.sqlite --send-test
python weekly_reports.py --db runtime/paper.sqlite --watch
python weekly_reports.py --db runtime/paper.sqlite --status
```

The default schedule in `reporting.json` is Monday at 09:00 in `America/Toronto`, covering the previous Monday-to-Monday calendar week. The sender uses Gmail SMTP over TLS on port 465 with an app password. Delivery attempts and ambiguous outcomes are recorded in a separate ledger. See [README_GMAIL.md](README_GMAIL.md) for report definitions and manual reconciliation.

### Gemini advisor

The model and request limits come from `advisor.json`. The checked-in model is `gemini-3.1-flash-lite`, with a local limit of 10 attempts per UTC day. Model availability and provider quotas must be checked for the configured Google project.

After a completed candle decision has saved advisor context:

```bash
python advisor.py --db runtime/paper.sqlite --input
python advisor.py --db runtime/paper.sqlite --once
python advisor.py --db runtime/paper.sqlite --show
```

For continuous observation, run `python advisor.py --db runtime/paper.sqlite --watch` in another terminal. `--input` and `--show` do not contact Gemini; `--once` and `--watch` can consume API quota. The adapter sends an allowlisted market snapshot and current mode, not account holdings or credentials.

`python test_gemini_connection.py` makes a real request using synthetic market data without a paper account. This standalone test bypasses the advisor ledger's daily cap.

Recommendations are logged separately and never applied automatically. See [README_GEMINI.md](README_GEMINI.md) for operational details; its older model example and historical pricing notes are not authoritative for the current configuration. `README_ADVISOR.md` describes the earlier advisor integration.

## Persistence and deployment

Preserve these files together when moving an account:

- `runtime/paper.sqlite`: account checkpoint, fills, events, and notification outbox.
- `runtime/paper.sqlite.reports.sqlite`: report delivery history, when reporting has been used.
- `runtime/paper.sqlite.advisor.sqlite`: advisor history and request accounting, when the advisor has been used.

The report and advisor ledgers are bound to the account path. Stop all workers and use `deploy/export_account.py` for a consistent export and path migration rather than copying a database while it is being written.

Docker Compose defines four services: `trader`, `notifier`, `reporter`, and `advisor`. The supplied stack enables all integrations and requires their credentials, a Sheets key, and an exported existing account in `docker-runtime/`. Preflight deliberately refuses to create a fresh account. See [README_DOCKER.md](README_DOCKER.md) for export, permissions, startup, and backup instructions. Provider pricing in that guide is historical.

The Docker build uses `requirements-docker.txt` and the existing service requirement files, independently of the native `requirements.txt`. Its healthcheck measures quote freshness, not the health of every strategy or service.

## Tests and regression checks

Run the maintained offline suite:

```bash
python -m unittest discover -s tests -v
```

The suite covers persistence, configuration changes, advisor validation, reporting, and deployment exports using temporary data and mocks. It does not establish profitability or replace real integration checks. `test_gemini_connection.py` is a separate network test, not part of this command.

Replay a saved backtest using its recorded forecasts and compare trades:

```bash
python verify_saved_run.py \
  --data data/RUN_ID/market_data.csv \
  --forecasts data/RUN_ID/volatility_history.csv \
  --expected-trades data/RUN_ID/trades.csv \
  --config data/RUN_ID/config_used.json \
  --db runtime/regression.sqlite
```

Replace `RUN_ID` with an actual completed run folder. Use a new regression database path when changing input files.

## Repository layout

| Path | Purpose |
| --- | --- |
| `main.py` | Historical backtest entry point |
| `paper_loop.py` | Persistent live-data paper runner |
| `market/` | Feeds, indicators, missing-data preparation, and volatility |
| `trading/` | Strategies, risk, simulated execution, and accounting |
| `services/` | Storage, configuration, notifications, reporting, and advisor adapters |
| `deploy/` | Account export, preflight, and quote healthcheck |
| `tests/` | Maintained automated tests |
| `diagnostics/` | Market-data and accounting diagnostics |
| `data/`, `data_diagnostics/` | Historical run outputs and diagnostics |
| `archive/` | Legacy implementations and experiments |

For an older account missing `strategy_mode`, stop its workers and use `python migrate_strategy_mode.py --db runtime/paper.sqlite --config config.json`. The migration backs up the database before adding the missing mode.
