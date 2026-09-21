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


# -----------------------------
# Load configuration
# -----------------------------

with open("config.json", "r") as file:
    config = json.load(file)


# -----------------------------
# Download BTC data
# -----------------------------

btc_data = get_btc_data()


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


# -----------------------------
# Create portfolio
# -----------------------------

portfolio = Portfolio(
    config["starting_budget"]
)


# -----------------------------
# Track previous DCA purchase
# -----------------------------

last_buy_price = None


# -----------------------------
# Walk through BTC history
# -----------------------------

portfolio_history = []
active_position = None

for timestamp, candle in btc_data.iterrows():

    current_price = candle["Close"]
    current_atr = candle["ATR"]
    short_sma = candle["SMA_20"]
    long_sma = candle["SMA_50"]

    if active_position is not None:

        if current_price <= active_position["stop_loss"]:

            portfolio.sell_active(
                current_price,
                timestamp
            )

            active_position = None

    if active_position is None:

        if should_open_swing_trade(
            short_sma,
            long_sma
        ):

            trade_successful = (
                portfolio.buy_active(
                    config["swing"]["trade_amount"],
                    current_price,
                    timestamp
                )
            )

            if trade_successful:

                stop_loss = (
                    calculate_stop_loss(
                        current_price,
                        current_atr,
                        config["atr"]["multiplier"]
                    )
                )

                active_position = {
                    "entry_price": current_price,
                    "stop_loss": stop_loss
                }

    if should_dca_buy(
        current_price,
        last_buy_price,
        config["dca"]["drop_threshold"]
    ):

        trade_successful = portfolio.buy_btc(
            config["dca"]["amount"],
            current_price,
            timestamp
        )

        if trade_successful:
            last_buy_price = current_price

    portfolio_value = portfolio.get_value(
        current_price
    )

    portfolio_history.append({
        "timestamp": timestamp,
        "btc_price": current_price,
        "cash": portfolio.cash,
        "btc": portfolio.dca_btc,
        "portfolio_value": portfolio_value
    })
    

history_df = pd.DataFrame(
    portfolio_history
)

history_df.to_csv(
    "data/portfolio_history.csv",
    index=False
)

print(
    "Portfolio history saved to "
    "data/portfolio_history.csv"
)

# -----------------------------
# Final results
# -----------------------------

final_price = btc_data.iloc[-1]["Close"]

final_value = portfolio.get_value(
    final_price
)


print()
print("FINAL RESULTS")
print("----------------")

print(
    f"Cash remaining: "
    f"${portfolio.cash:.2f}"
)

print(
    f"BTC owned: "
    f"{portfolio.dca_btc:.6f}"
)

print(
    f"Portfolio value: "
    f"${final_value:.2f}"
)

trades_df = pd.DataFrame(portfolio.trades)

trades_df.to_csv(
    "data/trades.csv",
    index=False
)

print()
print("Trades saved to data/trades.csv")

starting_value = config["starting_budget"]

total_return = (
    final_value - starting_value
) / starting_value

print(
    f"Total return: "
    f"{total_return * 100:.2f}%"
)