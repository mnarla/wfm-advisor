"""
nodes/trend_node.py — Trend signal node for the LangGraph sell-timing pipeline.

WHY THIS NODE EXISTS:
Raw price history is noisy day-to-day. This node fits a simple trend line over
the recent price history to answer "is this item's sell price rising, falling,
or flat" — and how strongly — so downstream reasoning isn't reacting to
single-day noise. Without quantified trend direction, the synthesis layer would
have to do its own statistical reasoning over raw timestamps and prices, which
is unreliable when handed to an LLM. This node pre-computes a clean, numeric
signal that the synthesis layer can treat as a trusted input.

Implementation note: Uses plain linear regression via scipy.stats.linregress.
TODO: Swap in RandomForestRegressor here if residuals analysis reveals the
price-vs-time relationship is non-linear (e.g. step-changes around vault/patch
events). RF would capture those structural breaks better than OLS, but adds
model complexity and requires hyperparameter tuning — don't reach for it until
OLS is demonstrably inadequate.
"""

import os
import sqlite3
import numpy as np
from scipy import stats
from datetime import datetime, timezone
from typing import Dict, Any

DB_PATH = os.getenv("DB_PATH", "db/wfm.db")

# Tunable thresholds
PCT_CHANGE_THRESHOLD = 10.0           # % change over 90 days for classification (rising/falling)
MIN_DATA_POINTS = 10                  # Fewer than this -> low_confidence regardless of R²
R2_CONFIDENCE_THRESHOLD = 0.25       # R² below this -> low_confidence


def _parse_timestamp(ts: str) -> float:
    """Convert ISO timestamp string to a POSIX float (seconds since epoch)."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts!r}")


def compute_trend_signal(item_id: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Fetches 90-day price history for item_id and fits a linear regression to
    compute price trend direction and confidence.

    Returns a dict with:
        signal: 'rising' | 'falling' | 'flat' | 'insufficient_data'
        slope: float — price change per day (in platinum)
        r_squared: float — fit quality (0.0–1.0)
        confidence: 'high' | 'low'
        current_price: float | None — most recent median_price
        pct_change_90d: float | None — estimated % price change over the window
        reasoning: str — human-readable summary
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT recorded_at, median_price
        FROM price_history
        WHERE item_id = ? AND stat_window = '90day' AND median_price IS NOT NULL
        ORDER BY recorded_at ASC
        """,
        (item_id,),
    )
    rows = cur.fetchall()

    if len(rows) < 2:
        return {
            "signal": "insufficient_data",
            "slope": None,
            "r_squared": None,
            "confidence": "low",
            "current_price": None,
            "mean_price": None,
            "pct_change_90d": None,
            "low_price_item": False,
            "reasoning": f"Insufficient price history ({len(rows)} data points) to compute trend.",
        }

    # Convert timestamps to day-offsets (day 0 = first date in the series)
    try:
        timestamps = np.array([_parse_timestamp(r["recorded_at"]) for r in rows])
    except ValueError as e:
        return {
            "signal": "insufficient_data",
            "slope": None,
            "r_squared": None,
            "confidence": "low",
            "current_price": None,
            "mean_price": None,
            "pct_change_90d": None,
            "low_price_item": False,
            "reasoning": f"Failed to parse timestamps: {e}",
        }

    prices = np.array([r["median_price"] for r in rows], dtype=float)
    day_offsets = (timestamps - timestamps[0]) / 86400.0  # seconds -> days

    # Linear regression: price = slope * day + intercept
    result = stats.linregress(day_offsets, prices)
    slope = result.slope           # platinum per day
    r_squared = result.rvalue ** 2

    current_price = float(prices[-1])
    mean_price = float(np.mean(prices))
    low_price_item = mean_price < 2.0
    n_days = day_offsets[-1] - day_offsets[0]

    # Compute percentage change directly from actual observed start/end prices (averaged over up to 3 points)
    k = min(3, len(prices))
    start_price = float(np.mean(prices[:k]))
    end_price = float(np.mean(prices[-k:]))
    if start_price > 0:
        pct_change_90d = ((end_price - start_price) / start_price) * 100
    else:
        pct_change_90d = 0.0

    # Confidence
    confidence = "high" if (len(rows) >= MIN_DATA_POINTS and r_squared >= R2_CONFIDENCE_THRESHOLD) else "low"

    # Classification: based on the scale-independent pct_change_90d
    if pct_change_90d > PCT_CHANGE_THRESHOLD:
        signal = "rising"
        reasoning = (
            f"Price rose ~{pct_change_90d:.1f}% over the last {int(n_days)} days "
            f"(slope={slope:.3f} pt/day, R²={r_squared:.2f}, {confidence} confidence)."
        )
    elif pct_change_90d < -PCT_CHANGE_THRESHOLD:
        signal = "falling"
        reasoning = (
            f"Price fell ~{abs(pct_change_90d):.1f}% over the last {int(n_days)} days "
            f"(slope={slope:.3f} pt/day, R²={r_squared:.2f}, {confidence} confidence)."
        )
    else:
        signal = "flat"
        reasoning = (
            f"Price trend is flat over the last {int(n_days)} days "
            f"(slope={slope:.3f} pt/day, R²={r_squared:.2f}, {confidence} confidence)."
        )

    return {
        "signal": signal,
        "slope": round(slope, 4),
        "r_squared": round(r_squared, 4),
        "confidence": confidence,
        "current_price": current_price,
        "mean_price": round(mean_price, 2),
        "pct_change_90d": round(pct_change_90d, 2),
        "low_price_item": low_price_item,
        "reasoning": reasoning,
    }


def trend_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: reads item_id from state and writes trend_signal back.
    Opens its own short-lived DB connection, matching the pattern used in vault_node.py.
    """
    item_id = state.get("item_id")
    if not item_id:
        return {
            "trend_signal": {
                "signal": "insufficient_data",
                "slope": None,
                "r_squared": None,
                "confidence": "low",
                "current_price": None,
                "mean_price": None,
                "pct_change_90d": None,
                "low_price_item": False,
                "reasoning": "No item_id in state.",
            }
        }

    conn = sqlite3.connect(DB_PATH)
    try:
        signal = compute_trend_signal(item_id, conn)
    finally:
        conn.close()

    return {"trend_signal": signal}


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        """
        SELECT item_id, url_slug, frame_name, component_type
        FROM items
        WHERE frame_name IN ('Loki Prime', 'Rhino Prime', 'Xaku Prime')
        ORDER BY frame_name, component_type
        """
    )
    items = cur.fetchall()

    print(f"{'Slug':<45} {'Signal':<20} {'Slope/day':>10} {'R²':>6} {'Conf':<6} {'% 90d':>7}  Reasoning")
    print("-" * 150)
    for item in items:
        result = compute_trend_signal(item["item_id"], conn)
        print(
            f"{item['url_slug']:<45} "
            f"{result['signal']:<20} "
            f"{str(result['slope']) if result['slope'] is not None else 'N/A':>10} "
            f"{str(result['r_squared']) if result['r_squared'] is not None else 'N/A':>6} "
            f"{result['confidence']:<6} "
            f"{str(result['pct_change_90d']) if result['pct_change_90d'] is not None else 'N/A':>7}  "
            f"{result['reasoning']}"
        )

    conn.close()
