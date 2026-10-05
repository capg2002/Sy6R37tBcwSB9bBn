# GARCH starter: what to add and where

This is a runnable research starting point, not a completed trading risk system.
It uses your uploaded main(3).py and existing Portfolio/strategy interfaces.
Your local main.py may contain newer changes: compare before replacing anything.

## 1. Put the files beside main.py

Add missing_data.py, volatility.py, requirements-garch.txt, and
main_garch_example.py to your existing project folder. Keep your existing
market_data.py, indicators.py, portfolio.py, strategy files, and config.json.

Install into the same Python environment that runs your project:

```powershell
python -m pip install -r requirements-garch.txt
```

You can run the example without renaming or overwriting your main.py:

```powershell
python main_garch_example.py
```

It downloads data through your existing get_btc_data() and creates a new data/run
folder. It simulates trading through your existing Portfolio; it places no real
exchange orders. The historical execution still has your existing simplified
fill assumptions and no added slippage model.

## 2. Add a configuration section

Add this as a top-level member of config.json, with a comma between it and the
existing members. Do not replace your other configuration.

```json
"garch": {
    "window_hours": 4320,
    "min_observed_returns": 500,
    "forecast_every_hours": 24,
    "n_paths": 5,
    "seed": 123
}
```

These are example settings, not calibrated recommendations. 4320 hours is 180
days. Increase it if you have more history. The warm-up counts adjacent observed
pairs, not missing hours turned into simulated data. Five conditional paths is
small for development: later examine sensitivity to more draws and seeds.

The 24-hour cadence means we log a ONE-HOUR forecast at every 24th grid row.
We do not reuse that forecast for the next 24 hours. Set cadence to 1 to forecast
each observed hour; multiple GARCH fits per hour over long backtests can be slow.
If a scheduled origin has missing OHLC, this starter skips that origin.

## 3. What missing_data.py does

prepare_hourly_data(raw): sorts timestamps, excludes incomplete candles, inserts
absent hourly rows, validates OHLC, and sets bad candles to NaN. It adds
observed_ohlc so main can distinguish executable observations from gaps. Duplicate
or ambiguous timestamps still raise because they need a different repair policy.
It does not verify requested start/end coverage or implement live feed retries.

sample_return_paths(close_history): builds conditional state-space simulations
for log prices. The baseline is a zero-drift Gaussian random walk with no
measurement error:

    x[t] = x[t-1] + eta[t], eta[t] ~ Normal(0, q)
    log(Close[t]) = x[t]

This is deliberately a simple state-space model you can inspect and replace.
Given consecutive OBSERVED endpoints d hours apart, their log-price difference
has variance d*q. The code estimates q from these endpoint differences, preserving
elapsed time, then uses statsmodels' conditional simulation smoother.

Observed closes remain fixed; only missing log prices vary across draws. Returns
are 100 * diff(log_price): 1.0 represents a 1% log return. Do not change their scale
without also updating volatility.py's variance conversion.

Why simulated paths instead of the smoothed mean? A smooth mean suppresses the
missing innovations' squared magnitude. Conditional draws retain uncertainty
under the specified state-space model. They do NOT recover the actual lost prices.

The simulation smoother conditions on the entire history passed to it. This is
permitted for a forecast at that history's final candle. Never pass all future
backtest rows and then use the reconstructed past to make earlier decisions.

## 4. What volatility.py does

forecast_volatility(history) requests conditional return paths, fits a zero-mean
Student-t GARCH(1,1) to each, and forecasts the next hour's variance. It averages
those variances, then takes the square root. Because fitting uses percentage
returns, variance is divided by 10,000 before converting to sigma.

Example output fields:

- status: ok, warming_up, or fit_failed.
- sigma_next_hour: decimal log-return standard deviation. 0.01 is about 1%.
- sigma_path_min/max: range of forecast sigmas across imputations; NOT a confidence interval.
- missing_hours: missing rows in the supplied history.
- fitted_paths: number of successful fitted paths.
- detail: failure explanation, when applicable.

No missing prices means one fit: all hypothetical imputations would be identical.
Optimizer failures produce a logged status, not a usable risk forecast. Missing
packages, broken configuration, and programming errors are not silently hidden.

This is a TWO-STAGE approximation. Gaussian random-walk imputation followed by
GARCH is not joint inference under a GARCH model. State variance and GARCH parameter
uncertainty are not fully propagated. If clustering during gaps is important,
compare richer imputation models or implement a joint missing-data model later.

## 5. Exact main.py edits represented by main_garch_example.py

1. Add imports for prepare_hourly_data and forecast_volatility.
2. Load config once, using BASE_DIR / 'config.json'.
3. After get_btc_data(), save the returned raw data, then call
   btc_data = prepare_hourly_data(btc_data).
   Replace the old missing-price and missing-hour exceptions with this step;
   leaving those exceptions in place would stop before the new logic runs.
4. Save metadata.json as well as config_used.json.
5. Restart ATR calculation within each uninterrupted valid-data segment.
   The existing rolling SMAs become unavailable across gaps until warmed up.
6. Before the trading loop initialize pending_dca, last_observed_close,
   execution_path_uncertain, volatility_history, and the GARCH settings.
7. Use enumerate(btc_data.iterrows()) so row_number can slice the history.
8. At the top of each iteration, handle missing OHLC: cancel unfilled pending
   orders, do not trade, record a stale valuation using the last observed close,
   and continue. If a swing position/order spans the gap, flag execution ambiguity.
9. Execute pending buys at observed opens. For DCA, update last_buy_price only
   if portfolio.buy_btc reports a successful fill.
10. After stop checks and before close-based signals, calculate/log volatility:

```python
if row_number % forecast_every == 0:
    history = btc_data["Close"].iloc[
        max(0, row_number + 1 - window_hours):row_number + 1
    ]
    estimate = forecast_volatility(
        history,
        min_observed_returns=garch_config.get("min_observed_returns", 500),
        n_paths=garch_config.get("n_paths", 5),
        seed=garch_config.get("seed", 123) + row_number,
    )
    volatility_history.append({
        "candle_open": timestamp,
        "available_at": timestamp + pd.Timedelta(hours=1),
        "horizon_hours": 1,
        **estimate.to_dict(),
    })
```

The final included row is row_number, the current completed candle. iloc's ending
index is exclusive, which explains the +1. Nothing uses the next candle's close.
For candles labelled by opening time, a 10:00 candle becomes available at 11:00.

11. At the close, schedule DCA with pending_dca; do not buy immediately at Close.
12. After the loop save volatility_history.csv. Use the last observed close for
    the final valuation and report stale values and execution uncertainty.

The full example includes all these edits so you do not have to assemble the
pieces blind. Preserve any more recent improvements in your own main.py.

## 6. How gaps affect your trade results

The program continues through gaps. Modelled prices are never used as fills or
as invented High/Low values. While data is absent, holdings remain unchanged in
this historical continuation scenario. At the next valid candle, stop checking
resumes using that observed Open and Low.

If a stop may have triggered inside a missing candle, the actual historical trade
path is unknown. execution_path_uncertain remains true for the rest of the run.
Missing periods also hide drawdown, even if only DCA holdings were open. Reported
P/L and drawdown are scenario/observed-grid results, not a fully verified backtest.
For live trading, exchange-side orders, fill reconciliation and stale-feed policies
need a separate implementation; this historical loop does not replace them.

## 7. What to do after it runs

First inspect volatility_history.csv: warm-up followed by ok forecasts; finite,
positive sigma; failures visible with explanations; higher imputation sensitivity
near gaps. Check horizons and scaling before using output to control trading.

Then mask known data deliberately in a separate evaluation experiment. Compare
forecasts with/without artificial gaps, repeating over calm and turbulent periods.
Evaluate forecasts out of sample; do not choose settings using future test data.
Compare multiple window lengths, imputation models and seeds.

Only after that add a risk rule to new SWING entries. Keep reserve and allocation
checks regardless of GARCH. If a required forecast is unavailable, an explicit
policy can skip new swing entries while continuing existing-position management.
Choose the risk horizon intentionally: an hourly forecast does not automatically
measure the risk of holding a swing position for several days.

## Validation of this starter

Tested with statsmodels 0.14.6 and arch 8.0.0. Conditional paths were reproducible
with a fixed seed and finite through an 11-hour gap. The complete example ran
with your uploaded portfolio/strategy modules on synthetic hourly data, including
an internal gap and a missing final candle, and passed the existing accounting
reconciliation. This is software verification, not validation of forecasting skill
or profitability on real BTC data.

API references:
- https://www.statsmodels.org/stable/generated/statsmodels.tsa.statespace.simulation_smoother.SimulationSmoothResults.html
- https://arch.readthedocs.io/en/latest/univariate/forecasting.html
