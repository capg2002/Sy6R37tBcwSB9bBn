import pandas as pd


def calculate_atr(df, period=14):

    previous_close = df["Close"].shift(1)

    high_low = df["High"] - df["Low"]

    high_previous_close = abs(df["High"] - previous_close)

    low_previous_close = abs(df["Low"] - previous_close)

    true_range = pd.concat([high_low, high_previous_close, low_previous_close],
                           axis=1).max(axis=1)

    df["ATR"] = true_range.rolling(period).mean()

    return df


def calculate_sma(
    df,
    period,
    column="Close"
):

    return (
        df[column]
        .rolling(period)
        .mean()
    )