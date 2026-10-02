class Portfolio:

    def __init__(self, starting_cash, dca_allocation, swing_allocation, reserve, fee_rate):

        self.starting_cash = starting_cash

        self.dca_cash = dca_allocation
        self.swing_cash = swing_allocation
        self.reserve_cash = reserve

        self.dca_cost = 0.0
        self.active_cost = 0.0
        self.realized_pnl = 0.0
        self.dca_btc = 0.0
        self.active_btc = 0.0

        self.fee_rate = fee_rate

        self.trades = []

    def buy_btc(
        self,
        dollar_amount,
        btc_price,
        timestamp,
        strategy="DCA"
    ):

        if dollar_amount > self.dca_cash:
            return False

        purchase_value = dollar_amount / (1 + self.fee_rate)
        fee = purchase_value * self.fee_rate

        btc_bought = purchase_value / btc_price

        self.dca_cash -= dollar_amount
        self.dca_cost += dollar_amount
        self.dca_btc += btc_bought

        trade = {
            "timestamp": timestamp,
            "strategy": strategy,
            "side": "BUY",
            "btc_price": btc_price,
            "usd_amount": dollar_amount,
            "btc_amount": btc_bought,
            "cash_remaining": self.cash,
            "btc_balance": self.dca_btc,
            "gross_usd": purchase_value,
            "fee_usd": fee,
            "cash_spent": dollar_amount,
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

        if dollar_amount > self.swing_cash:
            return False

        purchase_value = dollar_amount / (1 + self.fee_rate)
        fee = purchase_value * self.fee_rate

        btc_bought = purchase_value / btc_price

        self.swing_cash -= dollar_amount
        self.active_cost += dollar_amount
        self.active_btc += btc_bought

        trade = {
            "timestamp": timestamp,
            "strategy": "SWING",
            "side": "BUY",
            "btc_price": btc_price,
            "usd_amount": dollar_amount,
            "btc_amount": btc_bought,
            "cash_remaining": self.cash,
            "gross_usd": purchase_value,
            "fee_usd": fee,
            "cash_spent": dollar_amount
        }

        self.trades.append(trade)

        print(
            f"{timestamp} | SWING BUY | "
            f"${dollar_amount:.2f} at "
            f"${btc_price:.2f}"
        )

        return True

    @property
    def cash(self):
        return( self.dca_cash
               + self.swing_cash
               + self.reserve_cash)

    def sell_active(
        self,
        btc_price,
        timestamp
    ):

        if self.active_btc <= 0:
            return False

        btc_sold = self.active_btc

        gross_proceeds = btc_sold * btc_price
        fee = gross_proceeds * self.fee_rate
        net_proceeds = gross_proceeds - fee

        trade_pnl = net_proceeds - self.active_cost

        # Credit the sale once, after deducting its fee.
        self.swing_cash += net_proceeds

        # Record the closed position's profit or loss.
        self.realized_pnl += trade_pnl

        # The entire swing position has been sold.
        self.active_btc = 0.0
        self.active_cost = 0.0

        trade = {
            "timestamp": timestamp,
            "strategy": "SWING",
            "side": "SELL",
            "btc_price": btc_price,
            "usd_amount": net_proceeds,
            "btc_amount": btc_sold,
            "gross_usd": gross_proceeds,
            "fee_usd": fee,
            "net_proceeds": net_proceeds,
            "cash_remaining": self.cash,
            "realized_pnl": trade_pnl
        }

        self.trades.append(trade)

        print(
            f"{timestamp} | SWING SELL | "
            f"{btc_sold:.6f} BTC at ${btc_price:.2f} | "
            f"Net proceeds: ${net_proceeds:.2f} | "
            f"P/L: ${trade_pnl:.2f}"
        )

        return True