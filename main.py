"""
Historical backtest: DCA and swing entries, ATR stops, GARCH sizing/trailing.
main preserves a maintained historical backtest for the trading strategies. It does not deal with money nor
external applications.
"""

# Import market preparation, trading execution, persistence, and result-export dependencies.
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
from math import isfinite

import pandas as pd

from market.missing_data import prepare_hourly_data
from market.market_data import get_btc_data
from market.indicators import calculate_atr, calculate_sma

from trading.portfolio import Portfolio
from trading.models import EngineState
from trading.execution import ExecutionModel
from trading.engine import process_candle

from services.config_validation import validate_config
from services.storage import SQLiteStore

# Base directory to read and write files

BASE_DIR = Path(__file__).resolve().parent

# -----------------------------
# Load configuration
# -----------------------------

# Load configuration JSON from the project directory and close the file afterward.
with open(BASE_DIR / "config.json", "r") as file:
    config = json.load(file)

# Function that ensures config is in the expected format and throws error if not.
validate_config(config)

# Storing variable names for key configurations
exit_config = config["exits"]

garch_trailing_enabled = exit_config["garch_trailing_enabled"]
garch_trailing_multiplier = float(exit_config["garch_trailing_multiplier"])


# Reject a non-finite or nonpositive GARCH trailing-stop multiplier.
if not isfinite(garch_trailing_multiplier) or garch_trailing_multiplier <= 0:
    raise ValueError("garch_trailing_multiplier must be finite and positive.")

# Keeps track of whether backtest uses bearish or bullish entrance strategy in data tracking name
crossover_config = config["swing"]["crossover"]
# Create a timestamped run directory whose name records the crossover entry setting.
run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + crossover_config.upper()
run_dir = BASE_DIR / "data" / run_id
run_dir.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Download BTC data
# -----------------------------

# get_btc_data uses yFinance to download historical data spanning back the past year in hourly increments.
btc_data = get_btc_data()
# Stop when the market-data download contains no usable table of candles.
if btc_data is None or btc_data.empty:
    raise ValueError("The market-data download returned no candles.")

# Preserve the returned raw data before marking/reindexing invalid candles
# Save raw input, prepare hourly observations, and save the normalized input used for the run.
btc_data.to_csv(run_dir / "raw_market_data.csv")
btc_data = prepare_hourly_data(btc_data)

input_path = run_dir / "market_data.csv"
btc_data.to_csv(input_path)

# Hash for unique data trial storage
data_hash = hashlib.sha256(input_path.read_bytes()).hexdigest()

# Record the input checksum, candle range, row count, and ATR calculation settings.
metadata = {
    "run_id": run_id,
    "data_sha256": data_hash,
    "first_candle": str(btc_data.index.min()),
    "last_candle": str(btc_data.index.max()),
    "number_of_candles": len(btc_data),
    "atr_method": "simple rolling mean of true range",  # Tracks the "average true range"
    "atr_period": config["atr"]["period"],
}  # Specifies that the calculation occurs over a certain period

# Writes used metadata and config
# Write reproducibility metadata to the run directory.
with open(run_dir / "metadata.json", "w") as file:
    json.dump(metadata, file, indent=4)

# Save the exact configuration used by this backtest.
with open(run_dir / "config_used.json", "w") as file:
    json.dump(config, file, indent=4)

# -----------------------------
# Calculate indicators
# -----------------------------

# ATR restarts after each gap. An imputed close cannot reconstruct OHLC/ATR
btc_data["ATR"] = float("nan")
segment_ids = (~btc_data["observed_ohlc"]).cumsum()
# Compute ATR separately within each uninterrupted segment of observed OHLC data.
for _, segment in btc_data[btc_data["observed_ohlc"]].groupby(segment_ids):
    calculated = calculate_atr(segment.copy(), period=config["atr"]["period"])
    btc_data.loc[segment.index, "ATR"] = calculated["ATR"]

# Calculates the moving average for the closing price from 20 and 50 periods.
btc_data["SMA_20"] = calculate_sma(btc_data, 20)

btc_data["SMA_50"] = calculate_sma(btc_data, 50)

# Keep track of previous moving averages and closing price to accurately simulate when to enact a strategy
btc_data["PREV_SMA_20"] = btc_data["SMA_20"].shift(1)
btc_data["PREV_SMA_50"] = btc_data["SMA_50"].shift(1)
btc_data["PREV_CLOSE"] = btc_data["Close"].shift(1)

# -----------------------------
# Create portfolio
# -----------------------------

# Creating a portfolio and defining state based on the constructed classes on other documents. Further detail
# is shared in said documents.
# Construct the portfolio, initialize backtest storage, and restore its committed state.
portfolio = Portfolio(config["starting_budget"],
    config["dca"]["allocation"],
    config["swing"]["allocation"],
    config["reserve"],
    config["execution"]["fee_rate"],
    slippage_rate=config["execution"]["slippage_rate"],)

state = EngineState()
store = SQLiteStore(run_dir / 'trading.sqlite')
store.initialize(config, portfolio, state, mode='backtest', context={'data_sha256': data_hash})
portfolio, state, revision = store.restore()

# Actually executes the trading strategy
# Configure simulated execution fees and slippage.
execution = ExecutionModel(fee_rate=config["execution"]["fee_rate"],
    slippage_rate=config["execution"]["slippage_rate"],)

# Initialize data diagnostic tracking variablesx
# Collect portfolio, volatility, stop, risk, and fill outputs throughout the backtest.
portfolio_history = []
volatility_history = []
stop_history = []
risk_history = []
fills = []

# These defaults are for diagnostics, to ensure consistency
garch_config = config.get("garch", {})
forecast_every = int(garch_config.get("forecast_every_hours", 24))

print(f"Forecast interval: {forecast_every} hour(s)", flush=True)
print(f"Output folder: {run_dir}", flush=True)

# Makes sure the given configurations are valid
window_hours = int(garch_config.get("window_hours", 24 * 180))
# Reject invalid forecast cadence or history-window length.
if forecast_every < 1 or window_hours < 3:
    raise ValueError("Invalid GARCH cadence or window.")

risk_config = config["risk"]

# For now, since GARCH predicts the next hour's closing price
# Require fresh hourly forecasts for this backtest configuration.
if forecast_every != 1:
    raise ValueError("This sizing experiment requires a fresh forecast every hour.")

# -----------------------------
# Walk through BTC history
# -----------------------------

# This is the primary loop per candle. Enumarates data rows to run through
# Process historical candles in order using only data available up to each candle.
for row_number, (timestamp, candle) in enumerate(btc_data.iterrows()):

    event_key = 'candle:' + timestamp.isoformat()

    # Stores what candle and timestamp is being used
    # Skip a candle already committed to this run database.
    if store.contains(event_key):
        continue

    # Limit the decision history to the current candle and all preceding observations.
    history = btc_data.iloc[: row_number + 1]

    # process_candle is defined elsewhere. It presents the full logic for how trading strategies are determined.
    # Pass the candle and current account state to the shared trading engine.
    result = process_candle(timestamp=timestamp,
        candle=candle,
        history=history,
        row_number=row_number,
        portfolio=portfolio,
        state=state,
        execution=execution,
        config=config,)

    # Saves the result of one processing step into SQLite, updating the version number
    # Commit the completed processing step before reporting its fills.
    revision = store.commit(event_key, portfolio, state, result, revision)

    # Print only after the trade and resulting state have been committed
    # Print each newly committed fill with its strategy, execution price, and reason.
    for fill in result.fills:
        print(f'{fill.timestamp} | {fill.strategy} {fill.side} | '
            f'{fill.btc_amount:.8f} BTC at ${fill.fill_price:,.2f} | {fill.reason}',
            flush=True,)

    # Keeps track of the history of what was traded, for when strategies were exited, the predicted risk and volatility.
    # Append the step outputs to the diagnostic histories.
    fills.extend(result.fills)
    stop_history.extend(result.stop_events)
    risk_history.extend(result.risk_events)
    volatility_history.extend(result.volatility_events)

    # Retain a portfolio snapshot only when the engine produced one.
    if result.portfolio_snapshot is not None:
        portfolio_history.append(result.portfolio_snapshot)

# Saving diagnostics and portfolio information.
# Export stop updates with a fixed column order, including when the history is empty.
pd.DataFrame(stop_history,
    columns=[
        "decision_time",
        "effective_from",
        "close",
        "sigma_next_hour",
        "previous_stop",
        "updated_stop",
        "reason",
    ],).to_csv(run_dir / "stop_history.csv",
    index=False,)

# Export volatility diagnostics and assemble the portfolio valuation history.
pd.DataFrame(volatility_history).to_csv(run_dir / "volatility_history.csv", index=False)

history_df = pd.DataFrame(portfolio_history)

starting_value = config["starting_budget"]

# Calculate sampled drawdown relative to the running peak, bounded below by starting capital.
running_peak = history_df["portfolio_value"].cummax().clip(lower=starting_value)

history_df["drawdown"] = history_df["portfolio_value"] / running_peak - 1

max_drawdown = -history_df["drawdown"].min()

# Save the valuation history with its calculated drawdown.
history_df.to_csv(run_dir / "portfolio_history.csv", index=False)

print(f"Portfolio history saved to " f"{run_dir / 'portfolio_history.csv'}")

# -----------------------------
# Final results
# -----------------------------

# Use the last observed close for end valuation; report its age/limitations.
final_price = state.last_observed_close
# Reject final performance evaluation when no observed closing price is available.
if final_price is None:
    raise ValueError("No usable observed candle; no backtest could be evaluated.")

# Value remaining holdings at the most recent observed close.
final_value = portfolio.get_value(final_price)

# Tracks price of total unsold bitcoin from DCA
dca_unrealized_pnl = portfolio.dca_btc * final_price - portfolio.dca_cost

# Tracks price of total unsold bitcoin from swing
swing_unrealized_pnl = portfolio.active_btc * final_price - portfolio.active_cost

# Tracks the total value from DCA strategy
dca_value = portfolio.dca_cash + portfolio.dca_btc * final_price

# Tracks the total value from DCA strategy
swing_value = portfolio.swing_cash + portfolio.active_btc * final_price

# Track P&L from both strategies
dca_pnl = dca_value - config["dca"]["allocation"]
swing_pnl = swing_value - config["swing"]["allocation"]

# Total, tangible profit from portfolio at-large
# Compare total portfolio growth with the sum of realized and unrealized profit or loss.
account_profit = final_value - starting_value
recorded_profit = portfolio.realized_pnl + dca_unrealized_pnl + swing_unrealized_pnl

# Ensures dependent calculations preserve their relationships.
# Reject a mismatch between account growth and recorded realized plus unrealized profit.
if abs(account_profit - recorded_profit) > 0.000001:
    raise ValueError("Accounting mismatch: portfolio growth does not " "equal realized plus unrealized P/L.")

# Reject a mismatch between combined strategy profits and total account profit.
if abs(account_profit - (dca_pnl + swing_pnl)) > 0.000001:
    raise ValueError("Accounting mismatch: strategy profits do not " "add up to total portfolio profit.")

## Summarizes key points for the run, including:
#  The certainty of the execution path
#  How many observations were missing from the API pull
#  Whether or not stale data was used
#  The starting, finishing values, returns, maximum drawdown, total trades, how much was spent on fees
#  simulated slippage, and P&L statistics.
# Assemble performance metrics together with missing-data and execution-uncertainty disclosures.
summary = {
    "execution_path_uncertain": state.execution_path_uncertain,
    "missing_or_invalid_hours": int((~btc_data["observed_ohlc"]).sum()),
    "final_valuation_is_stale": bool(not btc_data.iloc[-1]["observed_ohlc"]),
    "performance_note": ("Missing hours prevent complete drawdown measurement; if execution_path_uncertain "
        "is true, trades/P&L are a continuation scenario, not verified historical execution."),
    "starting_value": starting_value,
    "final_value": final_value,
    "return_pct": (final_value / starting_value - 1) * 100,
    "max_drawdown_pct": max_drawdown * 100,
    "trade_count": len(portfolio.trades),
    "fees_paid": sum(trade["fee_usd"] for trade in portfolio.trades),
    "simulated_slippage_usd": sum(t["slippage_usd"] for t in portfolio.trades),
    "realized_pnl": portfolio.realized_pnl,
    "unrealized_pnl": dca_unrealized_pnl + swing_unrealized_pnl,
    "dca_pnl": dca_pnl,
    "swing_pnl": swing_pnl,
}

# Printing and saving the final results
# Save the final performance summary as indented JSON.
with open(run_dir / "summary.json", "w") as file:
    json.dump(summary, file, indent=4)

# Display the summary and final account balances.
print(json.dumps(summary, indent=4))

print()
print("FINAL RESULTS")
print("----------------")

print(f"Cash remaining: " f"${portfolio.cash:.2f}")

print(f"BTC owned: " f"{portfolio.dca_btc + portfolio.active_btc:.6f}")

print(f"Portfolio value: " f"${final_value:.2f}")

# Export the accounting trade history and risk events for later review.
trades_df = pd.DataFrame(portfolio.trades)

trades_df.to_csv(run_dir / "trades.csv", index=False)

print(f"Trades saved to {run_dir / 'trades.csv'}")

pd.DataFrame(risk_history).to_csv(run_dir / "risk_history.csv",
    index=False,)

# Calculate and display the total return relative to starting capital.
total_return = (final_value - starting_value) / starting_value

print(f"Total return: " f"{total_return * 100:.2f}%")

# Close SQL database management
# Close the backtest database connection.
store.close()
