import json

from market_data import get_btc_data
from indicators import calculate_atr
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

for timestamp, candle in btc_data.iterrows():

    current_price = candle["Close"]


    if should_dca_buy(
        current_price,
        last_buy_price,
        config["dca"]["drop_threshold"]
    ):

        portfolio.buy_btc(
            config["dca"]["amount"],
            current_price
        )

        last_buy_price = current_price


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
    f"{portfolio.btc:.6f}"
)

print(
    f"Portfolio value: "
    f"${final_value:.2f}"
)