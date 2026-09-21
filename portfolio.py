class Portfolio:

    def __init__(self, starting_cash):

        self.cash = starting_cash
        self.btc = 0

    def buy_btc(self, dollar_amount, btc_price):

        if dollar_amount > self.cash:
            print("Not enough cash.")
            return

        btc_bought = dollar_amount / btc_price

        self.cash -= dollar_amount
        self.btc += btc_bought

        print(
            f"Bought {btc_bought:.6f} BTC "
            f"for ${dollar_amount:.2f}"
        )

    def get_value(self, btc_price):

        btc_value = self.btc * btc_price

        total_value = self.cash + btc_value

        return total_value