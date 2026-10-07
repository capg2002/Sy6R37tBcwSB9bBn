"""Integration example based on uploaded main(3).py, not a live trading runner.
Compare with your newer local edits before adopting it. GARCH is diagnostic only.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib

import json
import pandas as pd

from market.missing_data import prepare_hourly_data
from market.volatility import forecast_volatility
from trading.risk import size_swing_purchase

from market.market_data import get_btc_data
from market.indicators import (
    calculate_atr,
    calculate_sma
)
from trading.swing_strategy import (
    should_open_swing_trade_bearish,
    should_open_swing_trade_bullish,
    calculate_stop_loss
)
from trading.portfolio import Portfolio
from trading.dca_strategy import should_dca_buy
from math import exp, isfinite


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
if crossover_config == "bear":
    run_id = datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%S_%fBEAR"
    )

if crossover_config == "bull":
    run_id = datetime.now(timezone.utc).strftime(
    "%Y%m%dT%H%M%S_%fBULL"
    )

run_dir = BASE_DIR / "data" / run_id
run_dir.mkdir(parents=True, exist_ok=True)

stop_history = []

# -----------------------------
# Download BTC data
# -----------------------------

btc_data = get_btc_data()

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
pending_swing_sell = None
pending_swing_buy = None
active_position = None

# Keep this initialization before the loop.
last_buy_price = None

pending_dca = None
last_observed_close = None
execution_path_uncertain = False
volatility_history = []
# These defaults are for learning/diagnostics, not optimized trading settings.
garch_config = config.get("garch", {})
forecast_every = int(garch_config.get("forecast_every_hours", 24))

print(f"Forecast interval: {forecast_every} hour(s)", flush=True)
print(f"Output folder: {run_dir}", flush=True)

window_hours = int(garch_config.get("window_hours", 24 * 180))
if forecast_every < 1 or window_hours < 3:
    raise ValueError("Invalid GARCH cadence or window.")

risk_config = config["risk"]
risk_history = []

if forecast_every != 1:
    raise ValueError(
        "This sizing experiment requires a fresh forecast every hour."
    )

for row_number, (timestamp, candle) in enumerate(btc_data.iterrows()):

    if not candle["observed_ohlc"]:
        # Cancel unfilled one-candle orders. An absent opening is not a fill.
        if pending_swing_buy is not None or active_position is not None:
            execution_path_uncertain = True
        pending_swing_buy = None
        pending_dca = None
        # Keep the position for a documented continuation scenario. We cannot
        # establish whether an active stop was triggered inside the missing hour.
        portfolio_history.append({
            "timestamp": timestamp,
            "btc_price": last_observed_close,
            "cash": portfolio.cash,
            "dca_cash": portfolio.dca_cash,
            "swing_cash": portfolio.swing_cash,
            "reserve_cash": portfolio.reserve_cash,
            "dca_btc": portfolio.dca_btc,
            "active_btc": portfolio.active_btc,
            "portfolio_value": (
                portfolio.get_value(last_observed_close)
                if last_observed_close is not None else portfolio.cash
            ),
            "valuation_is_stale": True,
            "execution_path_uncertain": execution_path_uncertain,
        })
        continue

    last_observed_close = candle["Close"]

    opening_price = candle["Open"]
    current_price = candle["Close"]
    current_atr = candle["ATR"]

    cur_short_sma = candle["SMA_20"]
    cur_long_sma = candle["SMA_50"]
    prev_short_sma = candle["PREV_SMA_20"]
    prev_long_sma = candle["PREV_SMA_50"]

    exited_this_candle = False
    # AT THE OPEN:
    # Execute a sell decision made at an earlier completed close.
    if pending_swing_sell is not None:
        if active_position is not None:
            exit_reason = pending_swing_sell["reason"]

            sale_successful = portfolio.sell_active(
                opening_price,
                timestamp,
            )

            if sale_successful:
                print(f"{timestamp} | EXIT REASON: {exit_reason}")

                active_position = None
                exited_this_candle = True
                pending_swing_sell = None

                # Prevent a previously queued buy from reopening immediately.
                pending_swing_buy = None
        else:
            # The position no longer exists.
            pending_swing_sell = None
    # -------------------------------------------------
    # 1. AT THE OPEN:
    # Execute the order saved after the previous close.
    # -------------------------------------------------

    if pending_dca is not None:
        if portfolio.buy_btc(pending_dca["amount"], opening_price, timestamp):
            last_buy_price = opening_price
        pending_dca = None

    if pending_swing_buy is not None and not exited_this_candle:

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

    # Diagnostic forecast made after THIS candle closes. The result describes
    # the next hour only; with cadence=24 we log daily, not reuse it for 24 hours.
    estimate = None
    
    if row_number % forecast_every == 0:
        history = btc_data["Close"].iloc[max(0, row_number + 1 - window_hours):row_number + 1]
        estimate = forecast_volatility(
            history,
            min_observed_returns=garch_config.get("min_observed_returns", 500),
            n_paths=garch_config.get("n_paths", 5),
            seed=garch_config.get("seed", 123) + row_number,
        )
        volatility_history.append({
            "candle_open": timestamp,
            "available_at": timestamp + pd.Timedelta(hours=1),
            "horizon_hours": 1,
            **estimate.to_dict(),
        })

    # AT THE CLOSE:
    # Tighten the stop for the NEXT candle.
    # The current candle's stop checks have already happened.

    if (
        garch_trailing_enabled
        and active_position is not None
        and estimate is not None
        and estimate.status == "ok"
    ):
        sigma = estimate.sigma_next_hour

        if sigma is not None and isfinite(sigma) and sigma > 0:
            previous_stop = active_position["stop_loss"]

            candidate_stop = current_price * exp(
                -garch_trailing_multiplier * sigma
            )

            next_stop = max(previous_stop, candidate_stop)

            active_position["stop_loss"] = next_stop

            if next_stop > previous_stop:
                stop_history.append({
                    "decision_time": timestamp + pd.Timedelta(hours=1),
                    "effective_from": timestamp + pd.Timedelta(hours=1),
                    "close": current_price,
                    "sigma_next_hour": sigma,
                    "previous_stop": previous_stop,
                    "updated_stop": next_stop,
                    "reason": "GARCH trailing stop raised",
                })

    indicator_values = candle[required_indicators]

    indicators_available = (
        indicator_values.notna().all()
        and not indicator_values.isin(
            [float("inf"), float("-inf")]
        ).any()
    )
    # AT THE CLOSE:
    # Check only the SMA values needed for this exit.
    sma_values = candle[
        ["PREV_SMA_20", "PREV_SMA_50", "SMA_20", "SMA_50"]
    ]

    smas_available = (
        sma_values.notna().all()
        and not sma_values.isin(
            [float("inf"), float("-inf")]
        ).any()
    )

    if (
        active_position is not None
        and pending_swing_sell is None
        and exit_config.get("bearish_crossover_enabled", False)
        and smas_available
    ):
        bearish_crossover = (
            prev_short_sma >= prev_long_sma
            and cur_short_sma < cur_long_sma
        )

        if bearish_crossover:
            pending_swing_sell = {
                "reason": "bearish_crossover",
                "signal_time": timestamp + pd.Timedelta(hours=1),
            }

    if (
        active_position is None
        and not exited_this_candle
        and indicators_available
        and current_atr > 0
    ):
        if crossover_config == "bear":
            if should_open_swing_trade_bearish(
                prev_short_sma,
                prev_long_sma,
                cur_short_sma,
                cur_long_sma,
            ):
                amount, reason = size_swing_purchase(
                    base_amount=config["swing"]["trade_amount"],
                    available_cash=portfolio.swing_cash,
                    estimate=estimate,
                    reference_sigma=risk_config["reference_hourly_sigma"],
                    minimum_amount=risk_config["minimum_swing_amount"],
                    enabled=risk_config["garch_sizing_enabled"],
                )

                risk_history.append({
                    "signal_candle_open": timestamp,
                    "decision_time": timestamp + pd.Timedelta(hours=1),
                    "forecast_status": (
                        estimate.status if estimate is not None else "unavailable"
                    ),
                    "sigma_next_hour": (
                        estimate.sigma_next_hour if estimate is not None else None
                    ),
                    "base_amount": config["swing"]["trade_amount"],
                    "swing_cash_available": portfolio.swing_cash,
                    "approved_amount": amount,
                    "garch_sizing_enabled": risk_config["garch_sizing_enabled"],
                    "reason": reason,
                })

                if amount > 0:
                    pending_swing_buy = {
                        "amount": amount,
                        "atr": current_atr,
                        "atr_multiplier": config["atr"]["multiplier"],
                    }
        if crossover_config == "bull":
            if should_open_swing_trade_bullish(
                prev_short_sma,
                prev_long_sma,
                cur_short_sma,
                cur_long_sma,
            ):
                amount, reason = size_swing_purchase(
                    base_amount=config["swing"]["trade_amount"],
                    available_cash=portfolio.swing_cash,
                    estimate=estimate,
                    reference_sigma=risk_config["reference_hourly_sigma"],
                    minimum_amount=risk_config["minimum_swing_amount"],
                    enabled=risk_config["garch_sizing_enabled"],
                )

                risk_history.append({
                    "signal_candle_open": timestamp,
                    "decision_time": timestamp + pd.Timedelta(hours=1),
                    "forecast_status": (
                        estimate.status if estimate is not None else "unavailable"
                    ),
                    "sigma_next_hour": (
                        estimate.sigma_next_hour if estimate is not None else None
                    ),
                    "base_amount": config["swing"]["trade_amount"],
                    "swing_cash_available": portfolio.swing_cash,
                    "approved_amount": amount,
                    "garch_sizing_enabled": risk_config["garch_sizing_enabled"],
                    "reason": reason,
                })

                if amount > 0:
                    pending_swing_buy = {
                        "amount": amount,
                        "atr": current_atr,
                        "atr_multiplier": config["atr"]["multiplier"],
                    }

    # -------------------------------------------------
    # 4. DCA: decide at the close, attempt execution at the next open.
    # -------------------------------------------------
    if pending_dca is None and should_dca_buy(
        current_price, last_buy_price, config["dca"]["drop_threshold"]
    ):
        if portfolio.dca_cash >= config["dca"]["amount"]:
            pending_dca = {"amount": config["dca"]["amount"]}

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
        "portfolio_value": portfolio.get_value(current_price),
        "valuation_is_stale": False,
        "execution_path_uncertain": execution_path_uncertain
    })


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
final_price = last_observed_close
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
    "execution_path_uncertain": execution_path_uncertain,
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