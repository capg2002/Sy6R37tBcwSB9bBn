"""Translate a volatility forecast into a proposed swing purchase amount."""

from math import floor, isfinite


def size_swing_purchase(
    base_amount,
    available_cash,
    estimate,
    reference_sigma,
    minimum_amount,
    enabled=True,
):
    """Return (cash_amount_including_fees, explanation).

    Require a valid current forecast in both enabled and disabled modes.
    This gives the comparison runs the same forecast-availability gate.
    """

    if not isfinite(base_amount) or base_amount <= 0:
        raise ValueError("base_amount must be finite and positive.")

    if not isfinite(available_cash) or available_cash < 0:
        raise ValueError("available_cash must be finite and nonnegative.")

    if not isfinite(reference_sigma) or reference_sigma <= 0:
        raise ValueError("reference_sigma must be finite and positive.")

    if not isfinite(minimum_amount) or minimum_amount <= 0:
        raise ValueError("minimum_amount must be finite and positive.")

    if minimum_amount > base_amount:
        raise ValueError("minimum_amount cannot exceed base_amount.")

    # No current forecast: skip this entry, not the whole trading loop.
    if estimate is None or estimate.status != "ok":
        return 0.0, "No valid current volatility forecast"

    sigma = estimate.sigma_next_hour

    if sigma is None or not isfinite(sigma) or sigma <= 0:
        return 0.0, "Invalid forecast volatility"

    multiplier = min(1.0, reference_sigma / sigma) if enabled else 1.0

    amount = min(
        available_cash,
        base_amount * multiplier,
    )

    # Round down so rounding cannot push spending above the cash cap.
    amount = floor(amount * 100) / 100

    # Skip tiny orders instead of increasing them to a minimum.
    if amount < minimum_amount:
        return 0.0, "Calculated purchase is below the minimum amount"

    return amount, f"Accepted with multiplier {multiplier:.4f}"