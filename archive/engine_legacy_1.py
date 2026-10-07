# engine.py
import uuid
from dataclasses import dataclass, field
from math import exp, isfinite

import pandas as pd

from trading.dca_strategy import should_dca_buy
from archive.models_legacy_1 import ActivePosition, OrderIntent
from trading.risk import size_swing_purchase
from trading.swing_strategy import (
    calculate_stop_loss,
    should_open_swing_trade_bearish,
    should_open_swing_trade_bullish,
)
from market.volatility import forecast_volatility


@dataclass
class CandleResult:
    fills: list = field(default_factory=list)
    stop_events: list = field(default_factory=list)
    risk_events: list = field(default_factory=list)
    volatility_events: list = field(default_factory=list)
    portfolio_snapshot: dict | None = None


def process_candle(
    timestamp,
    candle,
    history,
    row_number,
    portfolio,
    state,
    execution,
    config,
):
    """Process exactly ONE hourly candle.

    main.py owns the historical loop. This function owns trading behavior for
    one candle only: missing-data handling, open fills, intrabar stops, close
    decisions, and the resulting portfolio snapshot.
    """

    result = CandleResult()

    garch_config = config.get("garch", {})
    risk_config = config["risk"]
    exit_config = config["exits"]
    crossover_config = config["swing"]["crossover"]

    forecast_every = int(garch_config.get("forecast_every_hours", 24))
    window_hours = int(garch_config.get("window_hours", 24 * 180))

    if forecast_every < 1 or window_hours < 3:
        raise ValueError("Invalid GARCH cadence or window.")

    if forecast_every != 1:
        raise ValueError(
            "This sizing experiment requires a fresh forecast every hour."
        )

    garch_trailing_enabled = bool(
        exit_config.get("garch_trailing_enabled", False)
    )
    garch_trailing_multiplier = float(
        exit_config.get("garch_trailing_multiplier", 3.0)
    )

    if (
        not isfinite(garch_trailing_multiplier)
        or garch_trailing_multiplier <= 0
    ):
        raise ValueError(
            "garch_trailing_multiplier must be finite and positive."
        )

    required_indicators = [
        "SMA_20",
        "SMA_50",
        "PREV_SMA_20",
        "PREV_SMA_50",
        "ATR",
    ]

    # =========================================================
    # 0. MISSING / INVALID CANDLE
    # =========================================================
    if not candle["observed_ohlc"]:
        # An absent opening cannot fill a one-candle pending buy. If a swing
        # position existed, we also cannot know whether its stop was crossed.
        if (
            state.pending_swing_buy is not None
            or state.active_position is not None
        ):
            state.execution_path_uncertain = True

        state.pending_swing_buy = None
        state.pending_dca = None

        stale_price = state.last_observed_close

        result.portfolio_snapshot = {
            "timestamp": timestamp,
            "btc_price": stale_price,
            "cash": portfolio.cash,
            "dca_cash": portfolio.dca_cash,
            "swing_cash": portfolio.swing_cash,
            "reserve_cash": portfolio.reserve_cash,
            "dca_btc": portfolio.dca_btc,
            "active_btc": portfolio.active_btc,
            "portfolio_value": (
                portfolio.get_value(stale_price)
                if stale_price is not None
                else portfolio.cash
            ),
            "valuation_is_stale": True,
            "execution_path_uncertain": state.execution_path_uncertain,
        }
        return result

    # From here onward we have a real observed candle.
    state.last_observed_close = candle["Close"]

    opening_price = candle["Open"]
    current_price = candle["Close"]
    current_atr = candle["ATR"]

    cur_short_sma = candle["SMA_20"]
    cur_long_sma = candle["SMA_50"]
    prev_short_sma = candle["PREV_SMA_20"]
    prev_long_sma = candle["PREV_SMA_50"]

    exited_this_candle = False

    # =========================================================
    # 1. AT THE OPEN
    # =========================================================

    # 1A. Execute a swing sell decision made at the previous close.
    if state.pending_swing_sell is not None:
        if state.active_position is not None and portfolio.active_btc > 0:
            sell_order = state.pending_swing_sell

            fill = execution.fill_sell(
                order=sell_order,
                reference_price=opening_price,
                timestamp=timestamp,
                btc_amount=portfolio.active_btc,
            )

            if portfolio.apply_fill(fill):
                result.fills.append(fill)

                state.active_position = None
                state.pending_swing_sell = None
                state.pending_swing_buy = None
                exited_this_candle = True
        else:
            # A stale sell order cannot survive if there is no position.
            state.pending_swing_sell = None

    # 1B. Execute pending DCA buy at this candle's open.
    if state.pending_dca is not None:
        dca_order = state.pending_dca

        fill = execution.fill_buy(
            order=dca_order,
            reference_price=opening_price,
            timestamp=timestamp,
        )

        if portfolio.apply_fill(fill):
            result.fills.append(fill)
            state.last_buy_price = fill.fill_price

        # One execution attempt only.
        state.pending_dca = None

    # 1C. Execute pending swing buy at this candle's open.
    if state.pending_swing_buy is not None and not exited_this_candle:
        if state.active_position is None:
            swing_order = state.pending_swing_buy

            fill = execution.fill_buy(
                order=swing_order,
                reference_price=opening_price,
                timestamp=timestamp,
            )

            if portfolio.apply_fill(fill):
                result.fills.append(fill)

                stop_loss = calculate_stop_loss(
                    fill.fill_price,
                    swing_order.atr,
                    swing_order.atr_multiplier,
                )

                state.active_position = ActivePosition(
                    entry_price=fill.fill_price,
                    stop_loss=stop_loss,
                    stop_reason="atr_stop",
                )

        # One execution attempt only, successful or not.
        state.pending_swing_buy = None

    # =========================================================
    # 2. DURING THE CANDLE: ACTIVE STOP
    # =========================================================
    if state.active_position is not None:
        stop_price = state.active_position.stop_loss
        exit_price = None

        if opening_price <= stop_price:
            # Gap/open through the stop: use the real opening price.
            exit_price = opening_price
        elif candle["Low"] <= stop_price:
            # Stop crossed intrabar: simulate fill at the stop price.
            exit_price = stop_price

        if exit_price is not None:
            stop_order = OrderIntent(
                order_id=str(uuid.uuid4()),
                strategy="SWING",
                side="SELL",
                reason=state.active_position.stop_reason,
                signal_time=timestamp,
            )

            fill = execution.fill_sell(
                order=stop_order,
                reference_price=exit_price,
                timestamp=timestamp,
                btc_amount=portfolio.active_btc,
            )

            if portfolio.apply_fill(fill):
                result.fills.append(fill)
                state.active_position = None
                state.pending_swing_sell = None
                exited_this_candle = True

    # =========================================================
    # 3. AT THE CLOSE
    # =========================================================

    # 3A. Forecast volatility using history available through this close.
    estimate = None
    close_history = history["Close"].iloc[-window_hours:]

    if row_number % forecast_every == 0:
        estimate = forecast_volatility(
            close_history,
            min_observed_returns=garch_config.get(
                "min_observed_returns", 500
            ),
            n_paths=garch_config.get("n_paths", 5),
            seed=garch_config.get("seed", 123) + row_number,
        )

        result.volatility_events.append({
            "candle_open": timestamp,
            "available_at": timestamp + pd.Timedelta(hours=1),
            "horizon_hours": 1,
            **estimate.to_dict(),
        })

    # 3B. Tighten trailing stop for the NEXT candle only.
    if (
        garch_trailing_enabled
        and state.active_position is not None
        and estimate is not None
        and estimate.status == "ok"
    ):
        sigma = estimate.sigma_next_hour

        if sigma is not None and isfinite(sigma) and sigma > 0:
            previous_stop = state.active_position.stop_loss

            candidate_stop = current_price * exp(
                -garch_trailing_multiplier * sigma
            )

            next_stop = max(previous_stop, candidate_stop)
            state.active_position.stop_loss = next_stop

            if next_stop > previous_stop:
                state.active_position.stop_reason = "garch_trailing_stop"

                result.stop_events.append({
                    "decision_time": timestamp + pd.Timedelta(hours=1),
                    "effective_from": timestamp + pd.Timedelta(hours=1),
                    "close": current_price,
                    "sigma_next_hour": sigma,
                    "previous_stop": previous_stop,
                    "updated_stop": next_stop,
                    "reason": "GARCH trailing stop raised",
                })

    # 3C. Indicator availability checks.
    indicator_values = candle[required_indicators]
    indicators_available = (
        indicator_values.notna().all()
        and not indicator_values.isin(
            [float("inf"), float("-inf")]
        ).any()
    )

    sma_values = candle[
        ["PREV_SMA_20", "PREV_SMA_50", "SMA_20", "SMA_50"]
    ]
    smas_available = (
        sma_values.notna().all()
        and not sma_values.isin(
            [float("inf"), float("-inf")]
        ).any()
    )

    # 3D. Queue bearish-crossover exit for NEXT candle's open.
    if (
        state.active_position is not None
        and state.pending_swing_sell is None
        and exit_config.get("bearish_crossover_enabled", False)
        and smas_available
    ):
        bearish_crossover = (
            prev_short_sma >= prev_long_sma
            and cur_short_sma < cur_long_sma
        )

        if bearish_crossover:
            state.pending_swing_sell = OrderIntent(
                order_id=str(uuid.uuid4()),
                strategy="SWING",
                side="SELL",
                reason="bearish_crossover",
                signal_time=timestamp + pd.Timedelta(hours=1),
            )

    # 3E. Queue a new swing entry for NEXT candle's open.
    if (
        state.active_position is None
        and not exited_this_candle
        and indicators_available
        and current_atr > 0
    ):
        entry_signal = False
        entry_reason = None

        if crossover_config == "bear":
            entry_signal = should_open_swing_trade_bearish(
                prev_short_sma,
                prev_long_sma,
                cur_short_sma,
                cur_long_sma,
            )
            entry_reason = "bearish_crossover"

        elif crossover_config == "bull":
            entry_signal = should_open_swing_trade_bullish(
                prev_short_sma,
                prev_long_sma,
                cur_short_sma,
                cur_long_sma,
            )
            entry_reason = "bullish_crossover"

        else:
            raise ValueError(
                f"Unsupported swing crossover mode: {crossover_config}"
            )

        if entry_signal:
            amount, sizing_reason = size_swing_purchase(
                base_amount=config["swing"]["trade_amount"],
                available_cash=portfolio.swing_cash,
                estimate=estimate,
                reference_sigma=risk_config["reference_hourly_sigma"],
                minimum_amount=risk_config["minimum_swing_amount"],
                enabled=risk_config["garch_sizing_enabled"],
            )

            result.risk_events.append({
                "signal_candle_open": timestamp,
                "decision_time": timestamp + pd.Timedelta(hours=1),
                "forecast_status": (
                    estimate.status if estimate is not None else "unavailable"
                ),
                "sigma_next_hour": (
                    estimate.sigma_next_hour
                    if estimate is not None
                    else None
                ),
                "base_amount": config["swing"]["trade_amount"],
                "swing_cash_available": portfolio.swing_cash,
                "approved_amount": amount,
                "garch_sizing_enabled": risk_config["garch_sizing_enabled"],
                "reason": sizing_reason,
            })

            if amount > 0:
                state.pending_swing_buy = OrderIntent(
                    order_id=str(uuid.uuid4()),
                    strategy="SWING",
                    side="BUY",
                    reason=entry_reason,
                    signal_time=timestamp + pd.Timedelta(hours=1),
                    amount_usd=amount,
                    atr=current_atr,
                    atr_multiplier=config["atr"]["multiplier"],
                )

    # 3F. Queue DCA buy for NEXT candle's open.
    if (
        state.pending_dca is None
        and should_dca_buy(
            current_price,
            state.last_buy_price,
            config["dca"]["drop_threshold"],
        )
    ):
        if portfolio.dca_cash >= config["dca"]["amount"]:
            state.pending_dca = OrderIntent(
                order_id=str(uuid.uuid4()),
                strategy="DCA",
                side="BUY",
                reason="dca_drop_threshold",
                signal_time=timestamp + pd.Timedelta(hours=1),
                amount_usd=config["dca"]["amount"],
            )

    # =========================================================
    # 4. SNAPSHOT AT THIS CANDLE'S CLOSE
    # =========================================================
    result.portfolio_snapshot = {
        "timestamp": timestamp,
        "btc_price": current_price,
        "cash": portfolio.cash,
        "dca_cash": portfolio.dca_cash,
        "swing_cash": portfolio.swing_cash,
        "reserve_cash": portfolio.reserve_cash,
        "dca_btc": portfolio.dca_btc,
        "active_btc": portfolio.active_btc,
        "portfolio_value": portfolio.get_value(current_price),
        "valuation_is_stale": False,
        "execution_path_uncertain": state.execution_path_uncertain,
    }

    return result
