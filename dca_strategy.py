def should_dca_buy(
    current_price,
    last_buy_price,
    drop_threshold
):

    if last_buy_price is None:
        return True

    percentage_drop = (
        last_buy_price - current_price
    ) / last_buy_price

    if percentage_drop >= drop_threshold:
        return True

    return False