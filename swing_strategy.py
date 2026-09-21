def should_open_swing_trade(
    short_sma,
    long_sma
):

    if short_sma > long_sma:
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