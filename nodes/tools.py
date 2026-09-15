"""
nodes/tools.py — Specialized domain tools for the Phase 2 LangGraph tool-calling agent.

WHY THIS FILE EXISTS:
The existing pipeline nodes (trend_node, vault_node, patch_node, synthesis_node) are
hard-wired into a sequential StateGraph. Phase 2 exposes each domain function as a
LangChain @tool so that a Gemini-powered router can selectively call only the tools
required to answer a given query (vault-check, trend-check, fair-price, etc.).

Each tool:
  - Has a clear, intent-specific docstring that the LLM uses for tool selection.
  - Resolves item names through the existing slug_resolver.
  - Delegates to existing compute_* functions — no logic is duplicated.
  - Returns a plain string summary (the LLM reads text, not dicts).
"""

import os
import json
import sqlite3
import logging
from typing import Dict, Any, List

from langchain_core.tools import tool

from ingest.slug_resolver import resolve_item_query
from ingest.cache_manager import ensure_fresh_data
from nodes.trend_node import compute_trend_signal
from nodes.vault_node import compute_vault_signal
from nodes.patch_node import compute_patch_signal

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "db/wfm.db")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_and_freshen(item_name: str) -> tuple[str | None, str | None, list[str]]:
    """
    Resolves an item name to slugs and ensures fresh cached data.
    Returns (frame_name, component, slugs) — or (None, None, []) if not found.
    """
    resolved = resolve_item_query(item_name)
    if resolved.status != "resolved" or not resolved.slugs:
        return None, None, []
    ensure_fresh_data(resolved.slugs)
    return resolved.frame_name, resolved.component, resolved.slugs


def _fetch_item_row(slug: str, conn: sqlite3.Connection) -> Dict[str, Any] | None:
    """Fetches a single item row from the DB by url_slug."""
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM items WHERE url_slug = ?", (slug,))
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_48hr_prices(item_id: str, conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """
    Returns 48-hour closed-trade price history rows for an item.
    """
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT recorded_at, median_price, avg_price, volume, moving_avg
        FROM price_history
        WHERE item_id = ? AND stat_window = '48hr' AND median_price IS NOT NULL
        ORDER BY recorded_at DESC
        LIMIT 10
        """,
        (item_id,),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Tool 1: get_price_trend
# ---------------------------------------------------------------------------

@tool
def get_price_trend(item_name: str) -> str:
    """
    Fetches the 90-day statistical price trend for a Warframe Market item.
    Returns slope (plat/day), R² goodness-of-fit, percentage change over 90 days,
    and trend direction (rising, falling, or flat).
    Use this when the user asks whether an item is trending up or down in price,
    or as part of a broader sell/hold/buy analysis.
    """
    frame_name, component, slugs = _resolve_and_freshen(item_name)
    if not slugs:
        return f"Could not find item '{item_name}' in the Warframe Market database."

    conn = sqlite3.connect(DB_PATH)
    try:
        results = []
        for slug in slugs:
            row = _fetch_item_row(slug, conn)
            if not row:
                continue
            signal = compute_trend_signal(row["item_id"], conn)
            results.append(
                f"**{row['item_name']}** ({slug}):\n"
                f"  Direction: {signal['signal']}\n"
                f"  Slope: {signal['slope']} plat/day\n"
                f"  R²: {signal['r_squared']} ({signal['confidence']} confidence)\n"
                f"  90d Change: {signal['pct_change_90d']}%\n"
                f"  Current Price: {signal['current_price']}p\n"
                f"  Summary: {signal['reasoning']}"
            )
        return "\n\n".join(results) if results else f"No price data found for '{item_name}'."
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 2: get_vault_status
# ---------------------------------------------------------------------------

@tool
def get_vault_status(item_name: str) -> str:
    """
    Checks the vault status and Prime Resurgence schedule for a Warframe Prime item.
    Returns whether the item is currently vaulted, unvaulted, in Prime Resurgence,
    how many days since/until vaulting, and any resurgence expiry countdown.
    Use this when the user asks about vault status, resurgence schedules, or supply
    availability (e.g. 'Is Rhino Prime vaulted?', 'When does Loki Prime vault?').
    """
    frame_name, component, slugs = _resolve_and_freshen(item_name)
    if not slugs:
        return f"Could not find item '{item_name}' in the Warframe Market database."

    conn = sqlite3.connect(DB_PATH)
    try:
        # Only need to check the frame-level vault data (same for all parts)
        row = _fetch_item_row(slugs[0], conn)
        if not row:
            return f"No item record found for '{item_name}'."

        signal = compute_vault_signal(
            vault_status=row.get("vault_status", "unvaulted"),
            vault_date=row.get("vault_date"),
            estimated_vault_date=row.get("estimated_vault_date"),
            last_resurgence_end=row.get("last_resurgence_end"),
            is_resurgence_active=bool(row.get("is_resurgence_active", 0)),
            resurgence_end_date=row.get("resurgence_end_date"),
        )

        frame = row.get("frame_name", item_name)
        lines = [
            f"**{frame}** — Vault Status:",
            f"  Signal: {signal['signal']}",
            f"  Is Resurgence Active: {signal['is_resurgence_active']}",
        ]
        if signal.get("days_since_vaulted") is not None:
            lines.append(f"  Days Since Vaulted: {signal['days_since_vaulted']}")
        if signal.get("days_until_vault") is not None:
            lines.append(f"  Days Until Vault: {signal['days_until_vault']}")
        lines.append(f"  Summary: {signal['reasoning']}")
        return "\n".join(lines)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 3: get_patch_impact
# ---------------------------------------------------------------------------

@tool
def get_patch_impact(item_name: str) -> str:
    """
    Analyzes recent Warframe game balance patch notes for a specific item.
    Identifies whether the item received meaningful buffs, nerfs, or reworks
    in the past 90 days that could affect its market demand and price.
    Use this when the user asks what changed for an item in recent patches,
    or to understand if a price spike/drop is patch-related.
    """
    frame_name, component, slugs = _resolve_and_freshen(item_name)
    if not slugs:
        return f"Could not find item '{item_name}' in the Warframe Market database."

    conn = sqlite3.connect(DB_PATH)
    try:
        row = _fetch_item_row(slugs[0], conn)
        frame = (row.get("frame_name") if row else None) or frame_name or item_name
        signal = compute_patch_signal(frame, conn)

        if not signal.get("relevant_patch_found"):
            return (
                f"**{frame}**: No significant game balance changes found in the past 90 days.\n"
                f"  Patches checked: {signal.get('patchlogs_checked', 0)}\n"
                f"  Summary: {signal['reasoning']}"
            )

        impact = signal.get("expected_impact", "none")
        patch_name = signal.get("patch_name", "Unknown patch")
        impact_emoji = {"increase": "📈", "decrease": "📉", "unclear": "❓"}.get(impact, "")
        return (
            f"**{frame}**: Relevant patch found — {impact_emoji} **{impact.upper()}** expected.\n"
            f"  Patch: {patch_name}\n"
            f"  Patches checked: {signal.get('patchlogs_checked', 0)}\n"
            f"  Summary: {signal['reasoning']}"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 4: check_fair_price
# ---------------------------------------------------------------------------

@tool
def check_fair_price(item_name: str, offered_price: float) -> str:
    """
    Evaluates whether an offered price (in platinum) is fair, overpriced, or a bargain
    compared to recent 48-hour closed-trade median prices and 90-day moving averages.
    Use this when the user asks if a price is fair, reasonable, or if they're being scammed
    (e.g. 'Is 45p fair for Rhino Prime Blueprint?', 'Someone offered me 80p for X, good deal?').
    """
    frame_name, component, slugs = _resolve_and_freshen(item_name)
    if not slugs:
        return f"Could not find item '{item_name}' in the Warframe Market database."

    conn = sqlite3.connect(DB_PATH)
    try:
        results = []
        for slug in slugs:
            row = _fetch_item_row(slug, conn)
            if not row:
                continue

            prices_48hr = _fetch_48hr_prices(row["item_id"], conn)
            trend_signal = compute_trend_signal(row["item_id"], conn)

            current_median = trend_signal.get("current_price")
            mean_90d = trend_signal.get("mean_price")

            if not prices_48hr and current_median is None:
                results.append(
                    f"**{row['item_name']}**: No recent price data available for comparison."
                )
                continue

            # Use 48hr data if available, otherwise fall back to 90-day current
            if prices_48hr:
                recent_medians = [p["median_price"] for p in prices_48hr if p["median_price"]]
                market_price = sum(recent_medians) / len(recent_medians) if recent_medians else current_median
                lowest_48hr = min(recent_medians) if recent_medians else None
                highest_48hr = max(recent_medians) if recent_medians else None
            else:
                market_price = current_median
                lowest_48hr = None
                highest_48hr = None

            if market_price is None:
                results.append(f"**{row['item_name']}**: Unable to determine market price.")
                continue

            pct_diff = ((offered_price - market_price) / market_price) * 100

            if pct_diff > 20:
                verdict = f"⚠️ OVERPRICED by ~{abs(pct_diff):.0f}% vs. market median"
            elif pct_diff < -20:
                verdict = f"✅ BARGAIN — ~{abs(pct_diff):.0f}% below market median"
            else:
                verdict = f"✅ FAIR — within ±20% of market median"

            offered_str = f"{int(offered_price) if offered_price == int(offered_price) else offered_price}p"
            line = [
                f"**{row['item_name']}** ({slug}):",
                f"  Offered Price: {offered_str}",
                f"  48hr Market Median: {market_price:.0f}p",
                f"  90d Avg Price: {mean_90d:.1f}p" if mean_90d else None,
                f"  48hr Range: {lowest_48hr:.0f}p – {highest_48hr:.0f}p" if lowest_48hr else None,
                f"  Difference: {pct_diff:+.1f}%",
                f"  Verdict: {verdict}",
            ]
            results.append("\n".join(l for l in line if l))

        return "\n\n".join(results) if results else f"No price data found for '{item_name}'."
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 5: compare_set_vs_parts
# ---------------------------------------------------------------------------

@tool
def compare_set_vs_parts(item_name: str) -> str:
    """
    Compares the total price of buying all individual component parts for a Prime item
    against the price of buying or selling the complete assembled Set.
    Reveals whether selling parts individually or as a set is more profitable,
    and whether buyers are better off buying parts vs. the whole set.
    Use this when the user asks about set vs parts value, or whether to sell individually
    (e.g. 'Is it better to sell Rhino Prime as a set or parts?', 'set vs parts for Saryn Prime').
    """
    frame_name, component, slugs = _resolve_and_freshen(item_name)
    if not slugs:
        return f"Could not find item '{item_name}' in the Warframe Market database."

    conn = sqlite3.connect(DB_PATH)
    try:
        # Fetch all slugs for the frame (including set and all parts)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # If user queried the set, grab the resolved frame_name to get all parts
        if frame_name:
            cur.execute(
                "SELECT item_id, url_slug, item_name, component_type FROM items WHERE frame_name = ?",
                (frame_name,),
            )
        else:
            # Fall back to just the queried slugs
            cur.execute(
                "SELECT item_id, url_slug, item_name, component_type FROM items WHERE url_slug IN ({})".format(
                    ",".join("?" * len(slugs))
                ),
                slugs,
            )
        all_rows = [dict(r) for r in cur.fetchall()]

        set_row = next((r for r in all_rows if r["component_type"] == "set"), None)
        part_rows = [r for r in all_rows if r["component_type"] != "set"]

        if not set_row and not part_rows:
            return f"Could not retrieve item data for '{item_name}'."

        # Fetch prices
        def get_price(row: Dict[str, Any]) -> float | None:
            sig = compute_trend_signal(row["item_id"], conn)
            return sig.get("current_price")

        set_price = get_price(set_row) if set_row else None
        part_prices = {}
        for r in part_rows:
            p = get_price(r)
            if p is not None:
                part_prices[r["item_name"]] = (r["url_slug"], p)

        parts_total = sum(v for _, v in part_prices.values()) if part_prices else None

        lines = [f"**{frame_name or item_name}** — Set vs. Parts Breakdown:"]

        if set_price is not None:
            lines.append(f"  Set Price (current): {set_price:.0f}p")
        else:
            lines.append("  Set Price: No data")

        if part_prices:
            lines.append("  Individual Parts:")
            for name, (slug, price) in sorted(part_prices.items()):
                lines.append(f"    • {name}: {price:.0f}p")
            lines.append(f"  Sum of Parts: {parts_total:.0f}p")
        else:
            lines.append("  Individual Parts: No data")

        if set_price and parts_total:
            diff = parts_total - set_price
            if diff > 0:
                lines.append(
                    f"\n  💡 Selling parts individually yields ~{diff:.0f}p MORE than selling the set."
                )
            elif diff < 0:
                lines.append(
                    f"\n  💡 Selling as a set yields ~{abs(diff):.0f}p MORE than selling parts individually."
                )
            else:
                lines.append("\n  💡 Set and parts prices are roughly equivalent.")
        
        return "\n".join(lines)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool 6: get_market_signals
# ---------------------------------------------------------------------------

@tool
def get_market_signals(item_name: str) -> str:
    """
    Fetches all three market signals (price trend, vault status, and patch impact)
    for a Warframe Market item in a single call.
    Returns a comprehensive data bundle suitable for sell/hold, buy, or compare analysis.
    Use this when conducting a full analysis that requires all signals together, such as
    for sell/hold recommendations, buy decisions, or cross-item comparisons.
    """
    frame_name, component, slugs = _resolve_and_freshen(item_name)
    if not slugs:
        return f"Could not find item '{item_name}' in the Warframe Market database."

    conn = sqlite3.connect(DB_PATH)
    try:
        results = []
        checked_frames = set()

        for slug in slugs:
            row = _fetch_item_row(slug, conn)
            if not row:
                continue

            trend = compute_trend_signal(row["item_id"], conn)

            vault_signal = compute_vault_signal(
                vault_status=row.get("vault_status", "unvaulted"),
                vault_date=row.get("vault_date"),
                estimated_vault_date=row.get("estimated_vault_date"),
                last_resurgence_end=row.get("last_resurgence_end"),
                is_resurgence_active=bool(row.get("is_resurgence_active", 0)),
                resurgence_end_date=row.get("resurgence_end_date"),
            )

            # Only compute patch signal once per frame (same for all parts)
            frame = row.get("frame_name") or frame_name or item_name
            if frame not in checked_frames:
                patch = compute_patch_signal(frame, conn)
                checked_frames.add(frame)
            else:
                patch = {
                    "relevant_patch_found": False,
                    "expected_impact": "none",
                    "reasoning": "Patch data already computed for this frame.",
                }

            lines = [
                f"**{row['item_name']}** ({slug}):",
                f"  TREND → {trend['signal']} | {trend['pct_change_90d']}% over 90d | Current: {trend.get('current_price')}p",
                f"  VAULT → {vault_signal['signal']} | {vault_signal['reasoning']}",
                f"  PATCH → {patch.get('expected_impact','none')} | {patch['reasoning']}",
            ]
            results.append("\n".join(lines))

        return "\n\n".join(results) if results else f"No data found for '{item_name}'."
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tool registry — all tools used by the agent
# ---------------------------------------------------------------------------

ALL_MARKET_TOOLS = [
    get_price_trend,
    get_vault_status,
    get_patch_impact,
    check_fair_price,
    compare_set_vs_parts,
    get_market_signals,
]
