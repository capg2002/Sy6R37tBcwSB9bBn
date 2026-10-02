from pathlib import Path
from datetime import datetime, timezone
import hashlib

import json
import pandas as pd

from market_data import get_btc_data
from indicators import (
    calculate_atr,
    calculate_sma
)
from swing_strategy import (
    should_open_swing_trade,
    calculate_stop_loss
)
from portfolio import Portfolio
from dca_strategy import should_dca_buy


BASE_DIR = Path(__file__).resolve().parent

run_id = datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%S_%fZ"
)

run_dir = BASE_DIR / "data" / run_id
run_dir.mkdir(parents=True, exist_ok=True)

# -----------------------------
# Load configuration
# -----------------------------

with open(BASE_DIR / "config.json", "r") as file:
    config = json.load(file)

with open(run_dir / "config_used.json", "w") as file:
    json.dump(config, file, indent=4)


# -----------------------------
# Download BTC data
# -----------------------------

btc_data = get_btc_data()

required_columns = ["Open", "High", "Low", "Close"]

if btc_data is None or btc_data.empty:
    raise ValueError("No market data was returned.")

missing_columns = [
    column
    for column in required_columns
    if column not in btc_data.columns
]

if missing_columns:
    raise ValueError(
        f"Missing market-data columns: {missing_columns}"
    )

if not isinstance(btc_data.index, pd.DatetimeIndex):
    raise ValueError("Market data must have datetime timestamps.")

if btc_data.index.hasnans:
    raise ValueError("Some candle timestamps are missing.")

if btc_data.index.has_duplicates:
    raise ValueError("Duplicate candle timestamps found.")

btc_data = btc_data.sort_index()

# -------------------------------------------------
# Keep only completed hourly candles.
#
# Assumption: timestamps label candle OPENING times.
# A candle starting at 14:00 finishes at 15:00.
# -------------------------------------------------

if btc_data.index.tz is None:
    raise ValueError(
        "Market-data timestamps have no timezone. "
        "Confirm the source timezone before continuing."
    )

# Use UTC consistently.
btc_data.index = btc_data.index.tz_convert("UTC")

now = pd.Timestamp.now(tz="UTC")
candle_duration = pd.Timedelta(hours=1)

candle_end_times = btc_data.index + candle_duration
completed = candle_end_times <= now

removed_count = int((~completed).sum())
btc_data = btc_data.loc[completed].copy()

if removed_count > 0:
    print(
        f"Excluded {removed_count} candle(s) "
        "whose closing time has not yet arrived."
    )

if btc_data.empty:
    raise ValueError("No completed candles are available.")

# -------------------------------------------------
# Check that consecutive candles are one hour apart.
# -------------------------------------------------

time_gaps = btc_data.index.to_series().diff()

bad_gaps = (
    time_gaps.notna()
    & (time_gaps != candle_duration)
)

if bad_gaps.any():
    first_bad_timestamp = time_gaps.index[bad_gaps][0]
    gap_size = time_gaps.loc[first_bad_timestamp]

    raise ValueError(
        "Missing or irregular hourly candles: "
        f"the gap ending at {first_bad_timestamp} "
        f"is {gap_size}, rather than one hour."
    )

prices = btc_data[required_columns].apply(
    pd.to_numeric,
    errors="coerce"
)

invalid_prices = (
    prices.isna()
    | prices.isin([float("inf"), float("-inf")])
    | (prices <= 0)
)

if invalid_prices.any().any():
    raise ValueError(
        "Prices contain missing, nonnumeric, infinite, "
        "or nonpositive values."
    )

invalid_candles = (
    (prices["High"] < prices["Low"])
    | (prices["High"] < prices["Open"])
    | (prices["High"] < prices["Close"])
    | (prices["Low"] > prices["Open"])
    | (prices["Low"] > prices["Close"])
)

if invalid_candles.any():
    raise ValueError("Some candles have inconsistent OHLC prices.")

btc_data[required_columns] = prices

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

# -----------------------------
# Calculate indicators
# -----------------------------

btc_data = calculate_atr(
    btc_data,
    period=config["atr"]["period"]
)

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

required_indicators = [
    "SMA_20",
    "SMA_50",
    "PREV_SMA_20",
    "PREV_SMA_50",
    "ATR"
]

# -----------------------------
# Create portfolio
# -----------------------------

portfolio = Portfolio(
    config["starting_budget"],
    config["dca"]["allocation"],
    config["swing"]["allocation"],
    config["reserve"],
    config["execution"]["fee_rate"]
)


# -----------------------------
# Track previous DCA purchase
# -----------------------------

last_buy_price = None


# -----------------------------
# Walk through BTC history
# -----------------------------
portfolio_history = []
pending_swing_buy = None
pending_dca_buy = None
exited_this_candle = False
active_position = None

# Keep this initialization before the loop.
last_buy_price = None

for timestamp, candle in btc_data.iterrows():

    opening_price = candle["Open"]
    current_price = candle["Close"]
    current_atr = candle["ATR"]

    cur_short_sma = candle["SMA_20"]
    cur_long_sma = candle["SMA_50"]
    prev_short_sma = candle["PREV_SMA_20"]
    prev_long_sma = candle["PREV_SMA_50"]

    exited_this_candle = False

    # -------------------------------------------------
    # 1. AT THE OPEN:
    # Execute the order saved after the previous close.
    # -------------------------------------------------

        # Execute the DCA decision made at the previous close.
    if pending_dca_buy is not None:

        trade_successful = portfolio.buy_btc(
            pending_dca_buy["amount"],
            opening_price,
            timestamp
        )

        if trade_successful:
            # Remember the actual simulated purchase price.
            last_buy_price = opening_price

        # Give this order one execution attempt.
        pending_dca_buy = None

    if pending_swing_buy is not None:

        if active_position is None:

            trade_successful = portfolio.buy_active(
                pending_swing_buy["amount"],
                opening_price,
                timestamp
            )

            if trade_successful:

                stop_loss = calculate_stop_loss(
                    opening_price,
                    pending_swing_buy["atr"],
                    pending_swing_buy["atr_multiplier"]
                )

                active_position = {
                    "entry_price": opening_price,
                    "stop_loss": stop_loss
                }

        # This order gets one execution attempt.
        # Clear it even if insufficient cash prevented the buy.
        pending_swing_buy = None

    # -------------------------------------------------
    # 2. DURING THE CANDLE:
    # Check whether an active stop was crossed.
    # -------------------------------------------------

    if active_position is not None:

        stop_price = active_position["stop_loss"]
        exit_price = None

        if opening_price <= stop_price:
            # Price opened at/below the stop.
            # Assume execution at the opening price.
            exit_price = opening_price

        elif candle["Low"] <= stop_price:
            # Price reached the stop during the candle.
            # Assume execution at the stop price.
            exit_price = stop_price

        if exit_price is not None:

            sale_successful = portfolio.sell_active(
                exit_price,
                timestamp
            )

            if sale_successful:
                active_position = None
                exited_this_candle = True

    # -------------------------------------------------
    # 3. AT THE CLOSE:
    # Generate an order for the NEXT candle's opening.
    # -------------------------------------------------

    indicator_values = candle[required_indicators]

    indicators_available = (
        indicator_values.notna().all()
        and not indicator_values.isin(
            [float("inf"), float("-inf")]
        ).any()
    )

    if (
        active_position is None
        and not exited_this_candle
        and indicators_available
        and current_atr > 0
    ):

        if should_open_swing_trade(
            prev_short_sma,
            prev_long_sma,
            cur_short_sma,
            cur_long_sma
        ):

            pending_swing_buy = {
                "amount": config["swing"]["trade_amount"],
                "atr": current_atr,
                "atr_multiplier": config["atr"]["multiplier"]
            }

    # -------------------------------------------------
    # 4. AT THE CLOSE:
    # Schedule a DCA purchase for the next opening.
    # -------------------------------------------------

    if (
        portfolio.dca_cash >= config["dca"]["amount"]
        and should_dca_buy(
            current_price,
            last_buy_price,
            config["dca"]["drop_threshold"]
        )
    ):

        pending_dca_buy = {
            "amount": config["dca"]["amount"]
        }

    # -------------------------------------------------
    # 5. Record portfolio value at this candle's close.
    # -------------------------------------------------

    portfolio_value = portfolio.get_value(current_price)

    portfolio_history.append({
        "timestamp": timestamp,
        "btc_price": current_price,
        "cash": portfolio.cash,
        "dca_cash": portfolio.dca_cash,
        "swing_cash": portfolio.swing_cash,
        "reserve_cash": portfolio.reserve_cash,
        "dca_btc": portfolio.dca_btc,
        "active_btc": portfolio.active_btc,
        "portfolio_value": portfolio.get_value(current_price)
    })

        

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

final_price = btc_data.iloc[-1]["Close"]

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

starting_value = config["starting_budget"]

total_return = (
    final_value - starting_value
) / starting_value

print(
    f"Total return: "
    f"{total_return * 100:.2f}%"
)