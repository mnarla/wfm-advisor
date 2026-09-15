"""
discord_bot/embeds.py — Rich Discord Embed builders for WFM Advisor slash commands and alerts.
"""

from typing import Dict, Any, Optional

# Color constants
COLOR_SELL = 0xE74C3C       # Red
COLOR_HOLD = 0xF39C12       # Orange/Gold
COLOR_BUY = 0x2ECC71        # Green
COLOR_BLUE = 0x3498DB       # Blue
COLOR_PURPLE = 0x9B59B6     # Purple
COLOR_YELLOW = 0xF1C40F     # Yellow
COLOR_BLURPLE = 0x5865F2    # Discord Blurple

FOOTER_TEXT = "WFM Sell-Timing Advisor"


def build_advice_embed(item_rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds a Discord embed for full SELL/HOLD/BUY recommendation cards.
    Color:
      - SELL: 0xE74C3C
      - HOLD: 0xF39C12
      - BUY: 0x2ECC71
    Fields:
      - Price
      - 90d Trend
      - Vault
      - Patch
      - Action + Reasoning
    """
    # If wrapped in results list, unwrap first item
    if "recommendation" not in item_rec and "results" in item_rec and item_rec["results"]:
        item_rec = item_rec["results"][0]

    rec = str(item_rec.get("recommendation", "HOLD")).upper()
    if rec == "SELL":
        color = COLOR_SELL
    elif rec == "BUY":
        color = COLOR_BUY
    else:
        color = COLOR_HOLD

    item_name = item_rec.get("item_name", "Unknown Item")
    slug = item_rec.get("slug", "")
    title = f"📊 Recommendation: {item_name}"
    if slug:
        title += f" ({slug})"

    # Retrieve or format signal summary
    sig_sum = item_rec.get("signal_summary")
    if not sig_sum:
        trend_sig = item_rec.get("trend_signal") or {}
        vault_sig = item_rec.get("vault_signal") or {}
        patch_sig = item_rec.get("patch_signal") or {}
        try:
            from ingest.cache_manager import format_signal_summary
            sig_sum = format_signal_summary(trend_sig, vault_sig, patch_sig)
        except Exception:
            curr_price = trend_sig.get("current_price")
            sig_sum = {
                "price": f"{curr_price}p" if curr_price is not None else "N/A",
                "trend": str(trend_sig.get("pct_change_90d", "N/A")),
                "vault": str(vault_sig.get("signal", "Unknown")),
                "patch": str(patch_sig.get("expected_impact", "None")),
            }

    reasoning = item_rec.get("reasoning", "No specific reasoning provided.")

    fields = [
        {"name": "Price", "value": str(sig_sum.get("price", "N/A")), "inline": True},
        {"name": "90d Trend", "value": str(sig_sum.get("trend", "N/A")), "inline": True},
        {"name": "Vault", "value": str(sig_sum.get("vault", "N/A")), "inline": True},
        {"name": "Patch", "value": str(sig_sum.get("patch", "N/A")), "inline": True},
        {"name": "Action + Reasoning", "value": f"**{rec}** — {reasoning}"[:1024], "inline": False},
    ]

    return {
        "title": title[:256],
        "color": color,
        "fields": fields,
        "footer": {"text": FOOTER_TEXT},
    }


def build_fair_price_embed(item_name: str, offered_price: float, analysis_text: str) -> Dict[str, Any]:
    """
    Builds a Discord embed for fair-price check evaluations.
    Color:
      - BARGAIN: 0x2ECC71 (Green)
      - FAIR: 0x3498DB (Blue)
      - OVERPRICED: 0xE74C3C (Red)
    """
    upper = analysis_text.upper()
    if "BARGAIN" in upper:
        color = COLOR_BUY
    elif "OVERPRICED" in upper:
        color = COLOR_SELL
    elif "FAIR" in upper:
        color = COLOR_BLUE
    else:
        color = COLOR_BLUE

    price_str = f"{int(offered_price) if offered_price == int(offered_price) else offered_price}p"

    return {
        "title": f"💰 Fair Price Check: {item_name}"[:256],
        "description": analysis_text[:4000],
        "color": color,
        "fields": [
            {"name": "Offered Price", "value": f"`{price_str}`", "inline": True},
        ],
        "footer": {"text": FOOTER_TEXT},
    }


def build_parts_embed(item_name: str, breakdown_text: str) -> Dict[str, Any]:
    """
    Builds a Discord embed comparing set vs individual component parts.
    """
    return {
        "title": f"⚖️ Set vs. Parts Analysis: {item_name}"[:256],
        "description": breakdown_text[:4000],
        "color": COLOR_PURPLE,
        "footer": {"text": FOOTER_TEXT},
    }


def build_vault_embed(item_name: str, vault_text: str) -> Dict[str, Any]:
    """
    Builds a Discord embed displaying vault status, days, and resurgence countdown.
    """
    return {
        "title": f"🏛️ Vault Status: {item_name}"[:256],
        "description": vault_text[:4000],
        "color": COLOR_YELLOW,
        "footer": {"text": FOOTER_TEXT},
    }


def build_agent_embed(query: str, response_text: str) -> Dict[str, Any]:
    """
    Builds a general conversational response embed for agent interactions.
    """
    title = f"🤖 WFM Advisor: {query}" if query else "🤖 WFM Market Advisor"
    return {
        "title": title[:256],
        "description": response_text[:4000],
        "color": COLOR_BLURPLE,
        "footer": {"text": FOOTER_TEXT},
    }
