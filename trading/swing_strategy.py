def should_open_swing_trade_bullish(
        prev_short_sma,
        prev_long_sma,
        cur_short_sma,
        cur_long_sma
        ):

    if prev_short_sma <= prev_long_sma and cur_short_sma > cur_long_sma:
        return True

    return False

def should_open_swing_trade_bearish(
        prev_short_sma,
        prev_long_sma,
        cur_short_sma,
        cur_long_sma
        ):

    if prev_short_sma >= prev_long_sma and cur_short_sma < cur_long_sma:
        return True

    return False


def calculate_stop_loss(
    entry_price,
    atr,
    atr_multiplier
):

    stop_loss = (
        entry_price
        - atr_multiplier * atr
    )

    return stop_loss
