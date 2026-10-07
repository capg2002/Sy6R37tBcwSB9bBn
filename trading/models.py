# models.py
from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class ActivePosition:
    entry_price: float
    stop_loss: float
    stop_reason: str


@dataclass
class OrderIntent:
    order_id: str
    strategy: str
    side: str
    reason: str
    signal_time: Any
    amount_usd: Optional[float] = None
    atr: Optional[float] = None
    atr_multiplier: Optional[float] = None


@dataclass
class Fill:
    trade_id: str
    order_id: str
    timestamp: Any
    strategy: str
    side: str
    reason: str
    reference_price: float
    fill_price: float
    btc_amount: float
    gross_usd: float
    fee_usd: float
    net_usd: float


@dataclass
class EngineState:
    pending_dca: Optional[OrderIntent] = None
    pending_swing_buy: Optional[OrderIntent] = None
    pending_swing_sell: Optional[OrderIntent] = None
    active_position: Optional[ActivePosition] = None
    last_buy_price: Optional[float] = None
    last_observed_close: Optional[float] = None
    execution_path_uncertain: bool = False

    # Runtime watermarks are persisted alongside pending orders and stops.
    last_processed_candle: Optional[Any] = None
    last_quote_time: Optional[Any] = None
    last_exit_hour: Optional[Any] = None
