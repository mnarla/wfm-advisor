"""
nodes/vault_node.py — Vault & Prime Resurgence signal node for the LangGraph sell-timing pipeline.

WHY THIS NODE EXISTS:
Vaulting removes an item's relics from the drop tables, which typically causes
prices to rise over time as supply dries up while demand persists. Prime Resurgence
re-injects market supply in monthly rotations. This node translates raw vault dates
and live Prime Resurgence rotations into an actionable signal: whether an item is freshly
vaulted (price may still be climbing), recently vaulted via Prime Resurgence,
currently unvaulted in Resurgence (temporary supply influx), long-vaulted (price stabilized),
soon-to-vault, or unvaulted.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, Optional

# Tunable thresholds (days)
VAULTING_SOON_THRESHOLD_DAYS = 90      # If estimated vault date is within this many days, flag as vaulting_soon
RECENTLY_VAULTED_THRESHOLD_DAYS = 180  # If vault_date or resurgence end is within this many days, flag as recently_vaulted

DB_PATH = os.getenv("DB_PATH", "db/wfm.db")


def _parse_date(date_str: str | None) -> datetime | None:
    """Parse an ISO date string into a UTC datetime."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def compute_vault_signal(
    vault_status: str,
    vault_date: str | None,
    estimated_vault_date: str | None,
    last_resurgence_end: str | None = None,
    is_resurgence_active: bool = False,
    resurgence_end_date: str | None = None,
) -> Dict[str, Any]:
    """
    Core vault & resurgence signal computation. Accepts raw vault fields and returns a signal dict.
    Extracted as a standalone function so it can be unit-tested without a DB or graph.

    Returns a dict with keys:
        - signal: str — one of 'not_vaulted', 'vaulting_soon', 'recently_vaulted', 'long_vaulted'
        - days_since_vaulted: int | None
        - days_until_vault: int | None
        - is_resurgence_active: bool
        - reasoning: str — human-readable fragment for synthesis node
    """
    now_date = datetime.now(tz=timezone.utc).date()

    # Case 1: Frame is currently unvaulted in Prime Resurgence rotation
    if is_resurgence_active:
        end_date_str = resurgence_end_date or estimated_vault_date
        parsed_end = _parse_date(end_date_str)
        days_until = (parsed_end.date() - now_date).days if parsed_end else None
        days_str = f"in ~{days_until} days" if days_until is not None and days_until > 0 else "imminently"

        return {
            "signal": "vaulting_soon",
            "days_since_vaulted": None,
            "days_until_vault": days_until,
            "is_resurgence_active": True,
            "reasoning": (
                f"Currently unvaulted in Prime Resurgence rotation until {end_date_str} ({days_str}) — "
                f"temporary supply influx active; prepare for price stabilization after rotation closes."
            ),
        }

    # Case 2: Frame is vaulted (either originally or following a Prime Resurgence rotation)
    if vault_status == "vaulted":
        parsed_vault_date = _parse_date(vault_date)
        parsed_resurgence_date = _parse_date(last_resurgence_end)

        # Pick the most recent vault event: latest resurgence end vs original vault date
        effective_date = parsed_vault_date
        is_from_resurgence = False

        if parsed_resurgence_date:
            if not parsed_vault_date or parsed_resurgence_date.date() > parsed_vault_date.date():
                effective_date = parsed_resurgence_date
                is_from_resurgence = True

        if effective_date:
            days_since = (now_date - effective_date.date()).days
            if days_since <= RECENTLY_VAULTED_THRESHOLD_DAYS:
                signal = "recently_vaulted"
                if is_from_resurgence:
                    reasoning = (
                        f"Re-entered vault {days_since} days ago ({effective_date.date().isoformat()}) "
                        f"following Prime Resurgence rotation — supply recently cut off; "
                        f"price may still be climbing as market absorbs remaining supply."
                    )
                else:
                    reasoning = (
                        f"Vaulted {days_since} days ago ({effective_date.date().isoformat()}) — "
                        f"supply may still be declining; price could still be climbing."
                    )
            else:
                signal = "long_vaulted"
                if is_from_resurgence:
                    reasoning = (
                        f"Vaulted {days_since} days ago (last resurgence ended {effective_date.date().isoformat()}) — "
                        f"price likely stabilized long ago."
                    )
                else:
                    reasoning = (
                        f"Vaulted {days_since} days ago ({effective_date.date().isoformat()}) — "
                        f"price likely stabilized long ago."
                    )

            return {
                "signal": signal,
                "days_since_vaulted": days_since,
                "days_until_vault": None,
                "is_resurgence_active": False,
                "reasoning": reasoning,
            }
        else:
            # Vaulted but no date recorded
            return {
                "signal": "long_vaulted",
                "days_since_vaulted": None,
                "days_until_vault": None,
                "is_resurgence_active": False,
                "reasoning": "Item is vaulted but no vault date recorded — treating as long-vaulted.",
            }

    # Case 3: Regular unvaulted prime frame
    parsed_est = _parse_date(estimated_vault_date)
    if parsed_est:
        days_until = (parsed_est.date() - now_date).days
        if 0 <= days_until <= VAULTING_SOON_THRESHOLD_DAYS:
            days_str = f"in ~{days_until} days" if days_until > 0 else "today"
            return {
                "signal": "vaulting_soon",
                "days_since_vaulted": None,
                "days_until_vault": days_until,
                "is_resurgence_active": False,
                "reasoning": (
                    f"Estimated to vault {days_str} ({estimated_vault_date}) — "
                    f"watch for price movement as relics become unavailable."
                ),
            }
        elif days_until < 0 and abs(days_until) <= VAULTING_SOON_THRESHOLD_DAYS:
            return {
                "signal": "vaulting_soon",
                "days_since_vaulted": None,
                "days_until_vault": days_until,
                "is_resurgence_active": False,
                "reasoning": (
                    f"Estimated vault date ({estimated_vault_date}) passed ~{abs(days_until)} days ago — "
                    f"vaulting expected imminently."
                ),
            }

    # No estimated date, or far-off estimate (> VAULTING_SOON_THRESHOLD_DAYS)
    return {
        "signal": "not_vaulted",
        "days_since_vaulted": None,
        "days_until_vault": days_until if parsed_est else None,
        "is_resurgence_active": False,
        "reasoning": (
            "Item is not vaulted and no imminent vaulting detected — "
            "no vault-driven supply pressure."
        ),
    }


def vault_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: reads vault info from state['vault_info'] (or fetches from DB via item_id)
    and produces a vault_signal.

    Updates state with vault_signal dict containing signal, days, and reasoning.
    """
    vault_info = state.get("vault_info")
    item_id = state.get("item_id")

    if not vault_info and item_id:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT vault_status, vault_date, estimated_vault_date,
                   last_resurgence_end, is_resurgence_active, resurgence_end_date
            FROM items WHERE item_id = ?
            """,
            (item_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            vault_info = dict(row)

    if not vault_info:
        vault_info = {}

    vault_status = vault_info.get("vault_status", "unvaulted")
    vault_date = vault_info.get("vault_date")
    estimated_vault_date = vault_info.get("estimated_vault_date")
    last_resurgence_end = vault_info.get("last_resurgence_end")
    is_resurgence_active = bool(vault_info.get("is_resurgence_active", False))
    resurgence_end_date = vault_info.get("resurgence_end_date")

    signal = compute_vault_signal(
        vault_status=vault_status,
        vault_date=vault_date,
        estimated_vault_date=estimated_vault_date,
        last_resurgence_end=last_resurgence_end,
        is_resurgence_active=is_resurgence_active,
        resurgence_end_date=resurgence_end_date,
    )

    return {"vault_signal": signal}
