import yfinance as yf


def get_btc_data():

    btc = yf.download(
        "BTC-USD",
        period="1y",
        interval="1h"
    )

    return btc