"""One-hour GARCH forecasts. This module never submits a trade."""
from dataclasses import asdict, dataclass
import numpy as np
from arch import arch_model
from market.missing_data import sample_return_paths


@dataclass
class VolatilityEstimate:
    status: str
    sigma_next_hour: float | None = None
    sigma_path_min: float | None = None
    sigma_path_max: float | None = None
    missing_hours: int = 0
    fitted_paths: int = 0
    detail: str = ""

    def to_dict(self):
        return asdict(self)


def forecast_volatility(close_history, min_observed_returns=500, n_paths=5, seed=123):
    """Input ends at the CURRENT completed candle, never at the end of the dataset.

    Output sigma is in decimal log-return units: 0.01 means about 1% per hour.
    500 is an illustrative warm-up threshold, not a statistically optimal choice.
    All paths must converge; failures return a status rather than crash the loop.
    """
    missing = int(close_history.isna().sum())
    adjacent_observed = int((close_history.notna() & close_history.shift().notna()).sum())
    if adjacent_observed < min_observed_returns:
        return VolatilityEstimate("warming_up", missing_hours=missing)
    try:
        paths = sample_return_paths(close_history, n_paths=n_paths, seed=seed)
        variances = []
        for column in paths:
            model = arch_model(
                paths[column], mean="Zero", vol="GARCH", p=1, q=1,
                dist="t", rescale=False
            )
            result = model.fit(disp="off", show_warning=False)
            if result.convergence_flag != 0:
                return VolatilityEstimate(
                    "fit_failed", missing_hours=missing,
                    detail=f"Optimizer did not converge for {column}."
                )
            # Percentage returns were used for fitting: divide variance by 100^2.
            variance = float(result.forecast(horizon=1, reindex=False)
                             .residual_variance.iloc[-1, 0]) / 10_000
            if not np.isfinite(variance) or variance <= 0:
                raise ValueError("GARCH produced an invalid forecast variance.")
            variances.append(variance)
        return VolatilityEstimate(
            status="ok", sigma_next_hour=float(np.sqrt(np.mean(variances))),
            sigma_path_min=float(np.sqrt(min(variances))),
            sigma_path_max=float(np.sqrt(max(variances))),
            missing_hours=missing, fitted_paths=len(variances)
        )
    except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
        return VolatilityEstimate("fit_failed", missing_hours=missing, detail=str(exc))
