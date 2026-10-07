"""Historical backtest: DCA and swing entries, ATR stops, GARCH sizing/trailing.
This is the maintained backtest entry point, not a continuous live trader.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite

import pandas as pd

from market.missing_data import prepare_hourly_data
from market.market_data import get_btc_data
from market.indicators import calculate_atr, calculate_sma

from archive.portfolio_legacy_1 import Portfolio
from services.config_validation import validate_config

from archive.models_legacy_1 import EngineState
from archive.execution_legacy_1 import ExecutionModel
from archive.engine_legacy_1 import process_candle


BASE_DIR = Path(__file__).resolve().parent

# -----------------------------
# Load configuration
# -----------------------------

with open(BASE_DIR / "config.json", "r") as file:
    config = json.load(file)

validate_config(config)

exit_config = config["exits"]

garch_trailing_enabled = exit_config["garch_trailing_enabled"]
garch_trailing_multiplier = float(
    exit_config["garch_trailing_multiplier"]
)



if (
    not isfinite(garch_trailing_multiplier)
    or garch_trailing_multiplier <= 0
):
    raise ValueError(
        "garch_trailing_multiplier must be finite and positive."
    )

crossover_config = config["swing"]["crossover"]
run_id = (
    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    + "_" + crossover_config.upper()
)
run_dir = BASE_DIR / "data" / run_id
run_dir.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Download BTC data
# -----------------------------

btc_data = get_btc_data()
if btc_data is None or btc_data.empty:
    raise ValueError("The market-data download returned no candles.")

# Preserve the returned raw data before marking/reindexing invalid candles.
btc_data.to_csv(run_dir / "raw_market_data.csv")
btc_data = prepare_hourly_data(btc_data)

input_path = run_dir / "market_data.csv"
btc_data.to_csv(input_path)

data_hash = hashlib.sha256(
    input_path.read_bytes()
).hexdigest()

metadata = {
    "run_id": run_id,
    "data_sha256": data_hash,
    "first_candle": str(btc_data.index.min()),
    "last_candle": str(btc_data.index.max()),
    "number_of_candles": len(btc_data),
    "atr_method": "simple rolling mean of true range",
    "atr_period": config["atr"]["period"]
}
with open(run_dir / "metadata.json", "w") as file:
    json.dump(metadata, file, indent=4)

with open(run_dir / "config_used.json", "w") as file:
    json.dump(config, file, indent=4)
# -----------------------------
# Calculate indicators
# -----------------------------

# ATR restarts after each gap. An imputed close cannot reconstruct OHLC/ATR.
btc_data["ATR"] = float("nan")
segment_ids = (~btc_data["observed_ohlc"]).cumsum()
for _, segment in btc_data[btc_data["observed_ohlc"]].groupby(segment_ids):
    calculated = calculate_atr(segment.copy(), period=config["atr"]["period"])
    btc_data.loc[segment.index, "ATR"] = calculated["ATR"]

btc_data["SMA_20"] = calculate_sma(
    btc_data,
    20
)

btc_data["SMA_50"] = calculate_sma(
    btc_data,
    50
)

btc_data["PREV_SMA_20"] = btc_data["SMA_20"].shift(1)
btc_data["PREV_SMA_50"] = btc_data["SMA_50"].shift(1)
btc_data["PREV_CLOSE"] = btc_data["Close"].shift(1)

# -----------------------------
# Create portfolio
# -----------------------------

portfolio = Portfolio(
    config["starting_budget"],
    config["dca"]["allocation"],
    config["swing"]["allocation"],
    config["reserve"],
    config["execution"]["fee_rate"],
    slippage_rate=config["execution"]["slippage_rate"]
)

state = EngineState()

execution = ExecutionModel(
    fee_rate=config["execution"]["fee_rate"],
    slippage_rate=config["execution"]["slippage_rate"],
)

portfolio_history = []
volatility_history = []
stop_history = []
risk_history = []
fills = []

# These defaults are for learning/diagnostics, not optimized trading settings.
garch_config = config.get("garch", {})
forecast_every = int(garch_config.get("forecast_every_hours", 24))

print(f"Forecast interval: {forecast_every} hour(s)", flush=True)
print(f"Output folder: {run_dir}", flush=True)

window_hours = int(garch_config.get("window_hours", 24 * 180))
if forecast_every < 1 or window_hours < 3:
    raise ValueError("Invalid GARCH cadence or window.")

risk_config = config["risk"]

if forecast_every != 1:
    raise ValueError(
        "This sizing experiment requires a fresh forecast every hour."
    )

# -----------------------------
# Walk through BTC history
# -----------------------------

for row_number, (timestamp, candle) in enumerate(
    btc_data.iterrows()
):
    if row_number % 250 == 0:

        print(
            f"Processed {row_number:,} / {len(btc_data):,} candles "
            f"({row_number / len(btc_data):.1%})",
            flush=True,
        )

    history = btc_data.iloc[:row_number + 1]

    result = process_candle(
        timestamp=timestamp,
        candle=candle,
        history=history,
        row_number=row_number,
        portfolio=portfolio,
        state=state,
        execution=execution,
        config=config,
    )

    fills.extend(
        result.fills
    )

    for fill in result.fills:

        if fill.side == "BUY":

            print(
                f"{fill.timestamp} | "
                f"{fill.strategy} BUY | "
                f"${fill.net_usd:.2f} at "
                f"${fill.fill_price:.2f} | "
                f"{fill.btc_amount:.6f} BTC | "
                f"Reason: {fill.reason}",
                flush=True,
            )

        elif fill.side == "SELL":

            print(
                f"{fill.timestamp} | "
                f"{fill.strategy} SELL | "
                f"{fill.btc_amount:.6f} BTC at "
                f"${fill.fill_price:.2f} | "
                f"Net proceeds: ${fill.net_usd:.2f} | "
                f"Reason: {fill.reason}",
                flush=True,
            )

    stop_history.extend(
        result.stop_events
    )

    risk_history.extend(
        result.risk_events
    )

    volatility_history.extend(
        result.volatility_events
    )

    if result.portfolio_snapshot is not None:
        portfolio_history.append(
            result.portfolio_snapshot
        )


pd.DataFrame(
    stop_history,
    columns=[
        "decision_time",
        "effective_from",
        "close",
        "sigma_next_hour",
        "previous_stop",
        "updated_stop",
        "reason",
    ],
).to_csv(
    run_dir / "stop_history.csv",
    index=False,
)

pd.DataFrame(volatility_history).to_csv(run_dir / "volatility_history.csv", index=False)

history_df = pd.DataFrame(
    portfolio_history
)

starting_value = config["starting_budget"]

running_peak = (
    history_df["portfolio_value"]
    .cummax()
    .clip(lower=starting_value)
)




history_df["drawdown"] = (
    history_df["portfolio_value"] / running_peak - 1
)

max_drawdown = -history_df["drawdown"].min()

history_df.to_csv(
    run_dir / "portfolio_history.csv",
    index=False
)

print(
    f"Portfolio history saved to "
    f"{run_dir / 'portfolio_history.csv'}"
)

# -----------------------------
# Final results
# -----------------------------

# Use the last observed close for end valuation; report its age/limitations.
final_price = state.last_observed_close
if final_price is None:
    raise ValueError("No usable observed candle; no backtest could be evaluated.")

final_value = portfolio.get_value(
    final_price
)


dca_unrealized_pnl = (
    portfolio.dca_btc * final_price
    - portfolio.dca_cost
)

swing_unrealized_pnl = (
    portfolio.active_btc * final_price
    - portfolio.active_cost
)

dca_value = (
    portfolio.dca_cash
    + portfolio.dca_btc * final_price
)

swing_value = (
    portfolio.swing_cash
    + portfolio.active_btc * final_price
)

dca_pnl = dca_value - config["dca"]["allocation"]
swing_pnl = swing_value - config["swing"]["allocation"]

account_profit = final_value - starting_value

recorded_profit = (
    portfolio.realized_pnl
    + dca_unrealized_pnl
    + swing_unrealized_pnl
)

if abs(account_profit - recorded_profit) > 0.000001:
    raise ValueError(
        "Accounting mismatch: portfolio growth does not "
        "equal realized plus unrealized P/L."
    )

if abs(account_profit - (dca_pnl + swing_pnl)) > 0.000001:
    raise ValueError(
        "Accounting mismatch: strategy profits do not "
        "add up to total portfolio profit."
    )

summary = {
    "execution_path_uncertain": state.execution_path_uncertain,
    "missing_or_invalid_hours": int((~btc_data["observed_ohlc"]).sum()),
    "final_valuation_is_stale": bool(not btc_data.iloc[-1]["observed_ohlc"]),
    "performance_note": (
        "Missing hours prevent complete drawdown measurement; if execution_path_uncertain "
        "is true, trades/P&L are a continuation scenario, not verified historical execution."
    ),
    "starting_value": starting_value,
    "final_value": final_value,
    "return_pct": (
        final_value / starting_value - 1
    ) * 100,
    "max_drawdown_pct": max_drawdown * 100,
    "trade_count": len(portfolio.trades),
    "fees_paid": sum(
        trade["fee_usd"]
        for trade in portfolio.trades
    ),
    "simulated_slippage_usd": sum(t["slippage_usd"] for t in portfolio.trades),
    "realized_pnl": portfolio.realized_pnl,
    "unrealized_pnl": (
        dca_unrealized_pnl + swing_unrealized_pnl
    ),
    "dca_pnl": dca_pnl,
    "swing_pnl": swing_pnl
}

with open(run_dir / "summary.json", "w") as file:
    json.dump(summary, file, indent=4)

print(json.dumps(summary, indent=4))

print()
print("FINAL RESULTS")
print("----------------")

print(
    f"Cash remaining: "
    f"${portfolio.cash:.2f}"
)

print(
    f"BTC owned: "
    f"{portfolio.dca_btc + portfolio.active_btc:.6f}"
)

print(
    f"Portfolio value: "
    f"${final_value:.2f}"
)

trades_df = pd.DataFrame(portfolio.trades)

trades_df.to_csv(
    run_dir / "trades.csv",
    index=False
)

print(
    f"Trades saved to {run_dir / 'trades.csv'}"
)

pd.DataFrame(risk_history).to_csv(
    run_dir / "risk_history.csv",
    index=False,
)

starting_value = config["starting_budget"]

total_return = (
    final_value - starting_value
) / starting_value

print(
    f"Total return: "
    f"{total_return * 100:.2f}%"
)