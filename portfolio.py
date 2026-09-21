class Portfolio:

    def __init__(self, starting_cash):

        self.starting_cash = starting_cash
        self.cash = starting_cash

        self.dca_btc = 0
        self.active_btc = 0

        self.trades = []

    def buy_btc(
        self,
        dollar_amount,
        btc_price,
        timestamp,
        strategy="DCA"
    ):

        if dollar_amount > self.cash:
            return False

        btc_bought = dollar_amount / btc_price

        self.cash -= dollar_amount
        self.dca_btc += btc_bought

        trade = {
            "timestamp": timestamp,
            "strategy": strategy,
            "side": "BUY",
            "btc_price": btc_price,
            "usd_amount": dollar_amount,
            "btc_amount": btc_bought,
            "cash_remaining": self.cash,
            "btc_balance": self.dca_btc
        }

        self.trades.append(trade)

        print(
            f"{timestamp} | "
            f"Bought {btc_bought:.6f} BTC "
            f"for ${dollar_amount:.2f} "
            f"at ${btc_price:.2f}"
        )

        return True


    def get_value(self, btc_price):

        total_btc = (
            self.dca_btc
            + self.active_btc
        )

        btc_value = total_btc * btc_price

        return self.cash + btc_value

    def buy_active(
        self,
        dollar_amount,
        btc_price,
        timestamp
    ):

        if dollar_amount > self.cash:
            return False

        btc_bought = dollar_amount / btc_price

        self.cash -= dollar_amount
        self.active_btc += btc_bought

        trade = {
            "timestamp": timestamp,
            "strategy": "SWING",
            "side": "BUY",
            "btc_price": btc_price,
            "usd_amount": dollar_amount,
            "btc_amount": btc_bought,
            "cash_remaining": self.cash
        }

        self.trades.append(trade)

        print(
            f"{timestamp} | SWING BUY | "
            f"${dollar_amount:.2f} at "
            f"${btc_price:.2f}"
        )

        return True

    def sell_active(
        self,
        btc_price,
        timestamp
    ):

        if self.active_btc <= 0:
            return False

        btc_sold = self.active_btc

        usd_received = (
            btc_sold * btc_price
        )

        self.cash += usd_received
        self.active_btc = 0

        trade = {
            "timestamp": timestamp,
            "strategy": "SWING",
            "side": "SELL",
            "btc_price": btc_price,
            "usd_amount": usd_received,
            "btc_amount": btc_sold,
            "cash_remaining": self.cash
        }

        self.trades.append(trade)

        print(
            f"{timestamp} | SWING SELL | "
            f"{btc_sold:.6f} BTC at "
            f"${btc_price:.2f}"
        )

        return True