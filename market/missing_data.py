"""Hourly data preparation and conditional log-price paths for modelling only."""
import numpy as np
import pandas as pd
from statsmodels.tsa.statespace.structural import UnobservedComponents

OHLC = ["Open", "High", "Low", "Close"]


def prepare_hourly_data(raw, now = None):
    """Keep gaps on the hourly grid; mark unusable OHLC instead of inventing it.

    Assumes timestamps label candle OPEN times, as in the project's data feed.
    Missing/invalid prices are recoverable; ambiguous timestamps/schema are not.
    Coverage checks concern the returned span, not requested start/end dates.
    """
    if raw is None or raw.empty:
        raise ValueError("No data returned; retry the download before this function.")
    
    if not isinstance(raw.index, pd.DatetimeIndex): 
        raise ValueError("Expected a DatetimeIndex.")
    
    if raw.index.tz is None:
        raise ValueError("Localize timestamps to the provider's timezone first.")
    
    if raw.index.hasnans or raw.index.has_duplicates:
        raise ValueError("Missing/duplicate timestamps need explicit resolution.")
    
    if not set(OHLC).issubset(raw.columns):
        raise ValueError(f"Expected flat columns including {OHLC}.")
    
    data = raw.copy().sort_index().tz_convert("UTC")

    if not (data.index == data.index.floor("h")).all():
        raise ValueError("Expected candle opens exactly on the hour.")
    
    now = pd.Timestamp.now(tz = "UTC") if now is None else pd.Timestamp(now)

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    
    data = data.loc[data.index + pd.Timedelta(hours=1) <= now]

    if data.empty:
        raise ValueError("No completed candles available.")
    
    data = data.reindex(pd.date_range(data.index[0], data.index[-1], freq = "h"))
    prices = data[OHLC].apply(pd.to_numeric, errors = "coerce")
    valid = np.isfinite(prices).all(axis=1) & prices.gt(0).all(axis=1)
    valid &= prices.High.ge(prices[["Open", "Close", "Low"]].max(axis=1))
    valid &= prices.Low.le(prices[["Open", "Close", "High"]].min(axis=1))
    data["observed_ohlc"] = valid

    # Conservative starting rule: reject the entire candle if any OHLC is bad.
    data[OHLC] = prices.where(valid, np.nan)

    return data


def sample_return_paths(close, n_paths = 5, seed = 123):
    """Conditional draws using ONLY the history passed by the caller.

    Model: log(P_t) = x_t; x_t = x_(t-1) + eta_t, eta_t ~ N(0, q).
    No measurement error or drift. q is estimated from observed endpoints.
    This is a baseline imputation model, NOT a missing-data GARCH likelihood.
    Returns percentage log returns, preserving the hourly time grid.
    """
    if n_paths < 1:
        raise ValueError("n_paths must be at least 1.")
    
    if not isinstance(close.index, pd.DatetimeIndex):
        raise ValueError("Expected a DatetimeIndex.")
    
    if close.index.has_duplicates or not close.index.is_monotonic_increasing:
        raise ValueError("History must be ordered and unique.")
    
    if not (close.index.to_series().diff().dropna() == pd.Timedelta(hours = 1)).all():
        raise ValueError("Reindex hourly BEFORE modelling; do not drop missing rows.")
    
    close = pd.to_numeric(close, errors = "coerce").astype(float)

    if ((close.notna()) & (~np.isfinite(close) | close.le(0))).any():
        raise ValueError("Observed closes must be finite and positive.")
    
    first = close.first_valid_index()
    if first is None:
        raise ValueError("No observed prices yet.")
    
    close = close.loc[first:]  # No attempt to invent prehistory.
    log_price = np.log(close)
    observed = np.flatnonzero(log_price.notna().to_numpy())

    if len(observed) < 3:
        raise ValueError("Need at least three observed prices.")
    
    if log_price.notna().all():
        # Avoid fitting identical GARCH models when no imputation is needed.
        return pd.DataFrame({"path_0": 100 * log_price.diff().iloc[1:]})

    # For observed endpoints d hours apart: delta_log ~ N(0, d*q).
    changes = np.diff(log_price.iloc[observed].to_numpy())
    hours = np.diff(observed)
    q = float(np.mean(changes**2 / hours))

    if not np.isfinite(q) or q <= 0:
        raise ValueError("Cannot estimate positive state innovation variance.")

    model = UnobservedComponents(
        log_price, level=True, stochastic_level=True, irregular=False
    )
    # Condition on the first known price; avoid diffuse-initialization artifacts.
    model.initialize_known([float(log_price.iloc[0])], np.zeros((1, 1)))
    model.update([q])
    smoother = model.simulation_smoother(rng = seed)
    paths = {}

    for i in range(n_paths):
        # Conditional historical draw; NOT model.simulate(), which is a different task.
        smoother.simulate()
        simulated_log = pd.Series(smoother.simulated_state[0].copy(), index=close.index)
        simulated_log.loc[close.notna()] = log_price.loc[close.notna()]
        paths[f"path_{i}"] = 100 * simulated_log.diff().iloc[1:]

    return pd.DataFrame(paths)
