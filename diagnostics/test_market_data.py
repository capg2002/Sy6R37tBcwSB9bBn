"""Standalone BTC hourly-data diagnostic. Never imports or runs main.py.

Run: python test_market_data.py
Optional: python test_market_data.py --start 2025-10-27 --end 2025-10-30
Optional: python test_market_data.py --max-retries 0

Uses pandas and yfinance. Saves original responses and diagnostic reports;
never fills gaps, merges retry prices, or changes the trading project's files.
Assumes hourly candles are labelled by OPENING time. All analysis uses UTC.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

try:
    import pandas as pd
    import yfinance as yf
    
except ImportError as exc:
    raise SystemExit(
        f"Missing dependency: {exc.name}. Install with:\n"
        "python -m pip install pandas yfinance"
    ) from exc

HOUR = pd.Timedelta(hours=1)
OHLC = ["Open", "High", "Low", "Close"]


def valid_prices(frame):
    """Return one Boolean per row; NaN, infinity, or impossible OHLC fails."""

    p = frame[OHLC].apply(pd.to_numeric, errors="coerce")
    finite_positive = (p.notna() & ~p.isin([float("inf"), float("-inf")]) & (p > 0)).all(axis=1)
    consistent = (
        (p.High >= p.Low) & (p.High >= p.Open) & (p.High >= p.Close)
        & (p.Low <= p.Open) & (p.Low <= p.Close)
    )
    return finite_positive & consistent


def analyse(raw, folder, now):
    """Preserve raw rows, then report hourly coverage without repairing it."""

    raw.to_csv(folder / "raw_download.csv")
    if raw.empty:
        raise ValueError("Download returned no rows. Check provider errors in the terminal.")
    
    if not isinstance(raw.index, pd.DatetimeIndex):
        raise ValueError("Response does not have a DatetimeIndex.")
    
    if raw.index.tz is None:
        raise ValueError("Timestamps have no timezone; refusing to guess it.")
    
    if raw.index.hasnans:
        raise ValueError("Response has missing timestamps; raw data saved for inspection.")
    
    if isinstance(raw.columns, pd.MultiIndex) or not set(OHLC).issubset(raw.columns):
        raise ValueError("Expected flat Open/High/Low/Close columns; inspect raw_download.csv.")

    frame = raw.copy()
    frame.index = frame.index.tz_convert("UTC")
    was_sorted = frame.index.is_monotonic_increasing

    frame = frame.sort_index()
    duplicate_rows = frame.index.duplicated(keep=False)
    frame.loc[duplicate_rows].to_csv(folder / "duplicate_rows.csv")

    unfinished = frame.index + HOUR > now
    frame.loc[unfinished].to_csv(folder / "unfinished_candles.csv")
    complete = frame.loc[~unfinished].copy()

    if complete.empty:
        raise ValueError("No completed candles available.")

    valid = valid_prices(complete)
    complete.loc[~valid].to_csv(folder / "invalid_price_rows.csv")
    stamps = complete.index.unique().sort_values()
    off_hour = stamps[stamps != stamps.floor("h")]
    pd.DataFrame({"timestamp": off_hour}).to_csv(folder / "off_hour_timestamps.csv", index=False)

    gaps = []
    for previous, current in zip(stamps[:-1], stamps[1:]):
        elapsed = current - previous
        if elapsed != HOUR:
            missing = None
            if previous == previous.floor("h") and current == current.floor("h"):
                missing = max(0, int(elapsed / HOUR) - 1)
            gaps.append({"previous_timestamp": previous, "current_timestamp": current,
                         "gap_hours": elapsed / HOUR, "missing_candles": missing})
            
    gap_table = pd.DataFrame(gaps, columns=["previous_timestamp", "current_timestamp", "gap_hours", "missing_candles"])
    gap_table.to_csv(folder / "data_gaps.csv", index=False)

    # Coverage is within the returned completed-data span only.
    expected = pd.date_range(stamps.min().ceil("h"), stamps.max().floor("h"), freq="h")
    missing_stamps = expected.difference(stamps)
    pd.DataFrame({"missing_timestamp": missing_stamps}).to_csv(folder / "missing_hours.csv", index=False)
   
    summary = {
        "raw_rows": len(raw), "original_timezone": str(raw.index.tz),
        "originally_sorted": bool(was_sorted),
        "duplicate_rows_including_first": int(duplicate_rows.sum()),
        "unfinished_or_future_rows": int(unfinished.sum()),
        "invalid_completed_price_rows": int((~valid).sum()),
        "off_hour_timestamps": len(off_hour),
        "first_completed_open": str(stamps.min()), "last_completed_open": str(stamps.max()),
        "hours_since_last_completed_close": (now - (stamps.max() + HOUR)).total_seconds() / 3600,
        "irregular_intervals": len(gaps), "missing_hours_within_returned_span": len(missing_stamps),
        "scope": "Internal coverage only; does not prove requested start/end coverage or explain the cause of missing data.",
    }

    return summary, gap_table


def download(**kwargs):
    # keepna=True preserves empty provider rows that the normal default drops.
    return yf.download(
        "BTC-USD", interval="1h", multi_level_index=False,
        keepna=True, repair=False, auto_adjust=True, ignore_tz=False,
        progress=False, threads=False, timeout=30, **kwargs
    )


def retry_gaps(gaps, folder, limit):
    results = []
    for number, gap in enumerate(gaps.itertuples(index=False), start=1):
        if len(results) >= limit:
            break
        if pd.isna(gap.missing_candles) or gap.missing_candles <= 0:
            continue
        expected = pd.date_range(gap.previous_timestamp + HOUR, gap.current_timestamp - HOUR, freq="h")
        start = gap.previous_timestamp - pd.Timedelta(days=1)
        end = gap.current_timestamp + pd.Timedelta(days=1)
        record = {"gap_start": str(gap.previous_timestamp), "gap_end": str(gap.current_timestamp),
                  "expected_missing_hours": len(expected), "retry_start": str(start), "retry_end_exclusive": str(end)}
        print(f"Retrying gap ending {gap.current_timestamp} ({len(expected)} missing hours)...", flush=True)
        try:
            raw = download(start=start.to_pydatetime(), end=end.to_pydatetime())
            if raw is None or raw.empty:
                raise ValueError("Empty retry response; provider errors may be printed above.")
            raw.to_csv(folder / f"retry_{number:03d}_raw.csv")
            if not isinstance(raw.index, pd.DatetimeIndex) or raw.index.tz is None:
                raise ValueError("Retry response has invalid or timezone-free timestamps.")
            raw.index = raw.index.tz_convert("UTC")
            valid = valid_prices(raw)
            # Duplicated retry timestamps are not counted as clean recovery.
            usable = raw.index[valid & ~raw.index.duplicated(keep=False)]
            recovered = expected.intersection(usable)
            record.update(valid_hours_recovered=len(recovered),
                          status="all_recovered" if len(recovered) == len(expected) 
                          else "partly_recovered" if len(recovered) else "not_recovered")
        
        except Exception as exc:
            record.update(status="retry_failed", error=str(exc))

        results.append(record)
        if len(results) < limit:
            time.sleep(1)
    return results


def main():
    parser = argparse.ArgumentParser(description = __doc__)
    parser.add_argument("--period", 
                        default = "1y", 
                        help = "Default matches your project's one-year request.")
    parser.add_argument("--start", 
                        help = "Optional YYYY-MM-DD, interpreted as UTC.")
    parser.add_argument("--end", 
                        help = "Exclusive YYYY-MM-DD; requires --start.")
    parser.add_argument("--max-retries", 
                        type=int, 
                        default = 5, 
                        help = "Maximum targeted gap requests; 0 disables retries.")
    args = parser.parse_args()

    if bool(args.start) != bool(args.end):
        parser.error("Supply both --start and --end, or neither.")
        
    if args.max_retries < 0:
        parser.error("--max-retries must be nonnegative.")
    request = {"period": args.period}
    if args.start:
        start, end = pd.Timestamp(args.start, tz="UTC"), pd.Timestamp(args.end, tz="UTC")
        if start >= end:
            parser.error("--start must be earlier than --end.")
        request = {"start": start.to_pydatetime(), "end": end.to_pydatetime()}

    now = pd.Timestamp.now(tz="UTC")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    folder = Path(__file__).resolve().parent / "data_diagnostics" / stamp
    folder.mkdir(parents=True)
    report = {"symbol": "BTC-USD", "interval": "1h", "request": request,
              "download_started_utc": str(now), "pandas_version": pd.__version__,
              "yfinance_version": getattr(yf, "__version__", "unknown"),
              "keepna": True, "repair": False, "auto_adjust": True}
    code = 0
    try:
        print("Downloading raw BTC-USD hourly candles (no trading code involved)...", flush=True)
        raw = download(**request)
        if raw is None:
            raise ValueError("Provider returned None; check errors printed above.")
        summary, gaps = analyse(raw, folder, now)
        report.update(summary)
        print(json.dumps(summary, indent=2))
        if not gaps.empty:
            print("\nFirst 20 irregular intervals (complete list in data_gaps.csv):")
            print(gaps.head(20).to_string(index=False))
        report["gap_retries"] = retry_gaps(gaps, folder, args.max_retries)
        print("\nRetry results:")
        print(json.dumps(report["gap_retries"], indent=2))
    except Exception as exc:
        report["error"] = str(exc)
        print(f"\nDiagnostic stopped: {exc}")
        code = 1
    finally:
        (folder / "summary.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nReports saved to: {folder}")
        print("No prices were fabricated or merged. Unrecovered gaps do not prove a permanent provider outage.")
    return code


if __name__ == "__main__":
    sys.exit(main())
