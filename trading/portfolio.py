from services.config_validation import number, validate_allocations
from math import isclose


class Portfolio:

    def __init__(self, starting_cash, dca_allocation, swing_allocation, reserve, fee_rate, slippage_rate=0.0):

        validate_allocations(starting_cash, dca_allocation, swing_allocation, reserve)
        for name, value in [('fee_rate', fee_rate), ('slippage_rate', slippage_rate)]:
            number(value, name)
            if value >= 1:
                raise ValueError(f'{name} must be less than 1.')
        self.slippage_rate = slippage_rate
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
        self.applied_trade_ids = set()
        self.applied_order_ids = set()

    def buy_btc(
        self,
        dollar_amount,
        btc_price,
        timestamp,
        strategy="DCA"
    ):

        number(dollar_amount, 'dollar_amount', strict=True)
        number(btc_price, 'btc_price', strict=True)
        if dollar_amount > self.dca_cash:
            return False

        # Slippage changes the fill price; the requested budget still includes fees.
        reference_price = btc_price
        btc_price = reference_price * (1 + self.slippage_rate)
        number(btc_price, 'fill_price', strict=True)

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
            "reference_price": reference_price,
            "slippage_rate": self.slippage_rate,
            "usd_amount": dollar_amount,
            "btc_amount": btc_bought,
            "slippage_usd": btc_bought * (btc_price - reference_price),
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


    def apply_fill(self, fill):
        """Apply an already-simulated Fill to portfolio accounting.

        This is the new execution/accounting boundary used by engine.py.
        The legacy buy/sell methods remain temporarily for regression testing.
        """

        if fill.trade_id in self.applied_trade_ids or fill.order_id in self.applied_order_ids:
            return False
        if not fill.trade_id or not fill.order_id:
            raise ValueError('Fills require nonempty trade and order IDs.')
        for name in ('reference_price', 'fill_price', 'btc_amount', 'gross_usd', 'net_usd'):
            number(getattr(fill, name), name, strict=True)
        number(fill.fee_usd, 'fee_usd')
        if not isclose(fill.gross_usd, fill.btc_amount * fill.fill_price, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError('Fill gross does not equal quantity times price.')
        expected_net = fill.gross_usd + fill.fee_usd if fill.side == 'BUY' else fill.gross_usd - fill.fee_usd
        if not isclose(fill.net_usd, expected_net, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError('Fill cash amount does not reconcile with its fee.')
        if fill.side == 'SELL' and self.active_btc > 0 and not isclose(fill.btc_amount, self.active_btc, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError('This engine only supports full-position swing sells.')

        if fill.strategy == "DCA" and fill.side == "BUY":
            if fill.net_usd > self.dca_cash:
                return False

            self.dca_cash -= fill.net_usd
            self.dca_cost += fill.net_usd
            self.dca_btc += fill.btc_amount

            trade = {
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "timestamp": fill.timestamp,
                "strategy": fill.strategy,
                "side": fill.side,
                "reason": fill.reason,
                "btc_price": fill.fill_price,
                "reference_price": fill.reference_price,
                "slippage_rate": self.slippage_rate,
                "usd_amount": fill.net_usd,
                "btc_amount": fill.btc_amount,
                "slippage_usd": fill.btc_amount * (fill.fill_price - fill.reference_price),
                "cash_remaining": self.cash,
                "btc_balance": self.dca_btc,
                "gross_usd": fill.gross_usd,
                "fee_usd": fill.fee_usd,
                "cash_spent": fill.net_usd,
            }
            self.trades.append(trade)
            self.applied_trade_ids.add(fill.trade_id)
            self.applied_order_ids.add(fill.order_id)
            return True

        if fill.strategy == "SWING" and fill.side == "BUY":
            if fill.net_usd > self.swing_cash:
                return False
            if self.active_btc > 0:
                return False

            self.swing_cash -= fill.net_usd
            self.active_cost += fill.net_usd
            self.active_btc += fill.btc_amount

            trade = {
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "timestamp": fill.timestamp,
                "strategy": fill.strategy,
                "side": fill.side,
                "reason": fill.reason,
                "btc_price": fill.fill_price,
                "reference_price": fill.reference_price,
                "slippage_rate": self.slippage_rate,
                "usd_amount": fill.net_usd,
                "btc_amount": fill.btc_amount,
                "slippage_usd": fill.btc_amount * (fill.fill_price - fill.reference_price),
                "cash_remaining": self.cash,
                "gross_usd": fill.gross_usd,
                "fee_usd": fill.fee_usd,
                "cash_spent": fill.net_usd,
            }
            self.trades.append(trade)
            self.applied_trade_ids.add(fill.trade_id)
            self.applied_order_ids.add(fill.order_id)
            return True

        if fill.strategy == "SWING" and fill.side == "SELL":
            if self.active_btc <= 0:
                return False

            # The engine asks execution.py to sell the entire active position.
            trade_pnl = fill.net_usd - self.active_cost

            self.swing_cash += fill.net_usd
            self.realized_pnl += trade_pnl
            self.active_btc = 0.0
            self.active_cost = 0.0

            trade = {
                "trade_id": fill.trade_id,
                "order_id": fill.order_id,
                "timestamp": fill.timestamp,
                "strategy": fill.strategy,
                "side": fill.side,
                "exit_reason": fill.reason,
                "btc_price": fill.fill_price,
                "reference_price": fill.reference_price,
                "slippage_rate": self.slippage_rate,
                "usd_amount": fill.net_usd,
                "btc_amount": fill.btc_amount,
                "slippage_usd": fill.btc_amount * (fill.reference_price - fill.fill_price),
                "gross_usd": fill.gross_usd,
                "fee_usd": fill.fee_usd,
                "net_proceeds": fill.net_usd,
                "cash_remaining": self.cash,
                "realized_pnl": trade_pnl,
            }
            self.trades.append(trade)
            self.applied_trade_ids.add(fill.trade_id)
            self.applied_order_ids.add(fill.order_id)
            return True

        raise ValueError(
            f"Unsupported fill: {fill.strategy} {fill.side}"
        )

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

        number(dollar_amount, 'dollar_amount', strict=True)
        number(btc_price, 'btc_price', strict=True)
        if dollar_amount > self.swing_cash:
            return False
        if self.active_btc > 0:
            return False  # This runner supports one active swing position.

        # Slippage changes the fill price; the requested budget still includes fees.
        reference_price = btc_price
        btc_price = reference_price * (1 + self.slippage_rate)
        number(btc_price, 'fill_price', strict=True)

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
            "reference_price": reference_price,
            "slippage_rate": self.slippage_rate,
            "usd_amount": dollar_amount,
            "btc_amount": btc_bought,
            "slippage_usd": btc_bought * (btc_price - reference_price),
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
        timestamp,
        reason="manual"
    ):

        number(btc_price, 'btc_price', strict=True)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('A sale requires a nonempty reason.')
        if self.active_btc <= 0:
            return False

        reference_price = btc_price
        btc_price = reference_price * (1 - self.slippage_rate)
        number(btc_price, 'fill_price', strict=True)
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
            "exit_reason": reason,
            "btc_price": btc_price,
            "reference_price": reference_price,
            "slippage_rate": self.slippage_rate,
            "usd_amount": net_proceeds,
            "btc_amount": btc_sold,
            "slippage_usd": btc_sold * (reference_price - btc_price),
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