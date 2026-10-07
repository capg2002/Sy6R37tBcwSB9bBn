# execution.py
import uuid
from services.config_validation import number

from trading.models import Fill


class ExecutionModel:
    def __init__(self, fee_rate, slippage_rate=0.0):
        for name, value in [('fee_rate', fee_rate), ('slippage_rate', slippage_rate)]:
            number(value, name)
            if value >= 1:
                raise ValueError(f'{name} must be less than 1.')
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate

    def fill_buy(self, order, reference_price, timestamp):
        number(reference_price, 'reference_price', strict=True)
        number(order.amount_usd, 'amount_usd', strict=True)
        if order.side != 'BUY':
            raise ValueError('fill_buy requires a BUY intent.')
        if order.amount_usd is None or order.amount_usd <= 0:
            raise ValueError("A buy order requires a positive amount_usd.")

        fill_price = reference_price * (1 + self.slippage_rate)
        purchase_value = order.amount_usd / (1 + self.fee_rate)
        fee = purchase_value * self.fee_rate
        btc_amount = purchase_value / fill_price

        return Fill(
            trade_id=str(uuid.uuid4()),
            order_id=order.order_id,
            timestamp=timestamp,
            strategy=order.strategy,
            side="BUY",
            reason=order.reason,
            reference_price=reference_price,
            fill_price=fill_price,
            btc_amount=btc_amount,
            gross_usd=purchase_value,
            fee_usd=fee,
            net_usd=order.amount_usd,
        )

    def fill_sell(self, order, reference_price, timestamp, btc_amount):
        number(reference_price, 'reference_price', strict=True)
        number(btc_amount, 'btc_amount', strict=True)
        if order.side != 'SELL':
            raise ValueError('fill_sell requires a SELL intent.')
        if btc_amount <= 0:
            raise ValueError("A sell fill requires a positive btc_amount.")

        fill_price = reference_price * (1 - self.slippage_rate)
        gross = btc_amount * fill_price
        fee = gross * self.fee_rate
        net = gross - fee

        return Fill(
            trade_id=str(uuid.uuid4()),
            order_id=order.order_id,
            timestamp=timestamp,
            strategy=order.strategy,
            side="SELL",
            reason=order.reason,
            reference_price=reference_price,
            fill_price=fill_price,
            btc_amount=btc_amount,
            gross_usd=gross,
            fee_usd=fee,
            net_usd=net,
        )
