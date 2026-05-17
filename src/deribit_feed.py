"""
Deribit OHLC + DVOL puller. Public endpoints only — no auth required.

Pulls last N hours of hourly bars on each call. Returns a DataFrame with
columns: datetime, open, high, low, close, dvol.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

import numpy as np
import pandas as pd

from . import config


def _get(url: str, retries: int = 3, timeout: int = 15):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)


def fetch_ohlc(symbol: str = "ETH", hours: int | None = None) -> pd.DataFrame:
    hours = hours or config.DERIB.history_hours
    end_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    inst = f"{symbol}-PERPETUAL"
    url = (f"{config.DERIB.base_url}/public/get_tradingview_chart_data"
           f"?instrument_name={inst}"
           f"&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution=60")
    j = _get(url)["result"]
    if not j.get("ticks"):
        raise RuntimeError(f"Empty OHLC response for {inst}")
    df = pd.DataFrame({
        "ts":    j["ticks"],
        "open":  j["open"],
        "high":  j["high"],
        "low":   j["low"],
        "close": j["close"],
    })
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    return df.drop(columns=["ts"])


def fetch_dvol(currency: str = "ETH", hours: int | None = None) -> pd.DataFrame:
    hours = hours or config.DERIB.history_hours
    end_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    start_ms = end_ms - hours * 3600 * 1000
    url = (f"{config.DERIB.base_url}/public/get_volatility_index_data"
           f"?currency={currency}"
           f"&start_timestamp={start_ms}&end_timestamp={end_ms}&resolution=3600")
    j = _get(url)["result"]
    if not j.get("data"):
        raise RuntimeError(f"Empty DVOL response for {currency}")
    arr = np.array(j["data"])
    df = pd.DataFrame({
        "ts":    arr[:, 0],
        "dvol":  arr[:, 4],   # close
    })
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_localize(None)
    return df[["datetime", "dvol"]]


def fetch_combined(symbol: str = "ETH", hours: int | None = None) -> pd.DataFrame:
    """OHLC + DVOL merged on hourly grid."""
    ohlc = fetch_ohlc(symbol, hours)
    dvol = fetch_dvol(symbol, hours)
    return pd.merge_asof(
        ohlc.sort_values("datetime"),
        dvol.sort_values("datetime"),
        on="datetime",
        direction="nearest",
        tolerance=pd.Timedelta("90min"),
    )


if __name__ == "__main__":
    # Standalone test
    df = fetch_combined("ETH", hours=168)
    print(df.tail())
    print(f"\nrows: {len(df)}")
    print(f"missing DVOL: {df['dvol'].isna().sum()}")
