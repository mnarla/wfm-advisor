import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from ingest.slug_utils import parse_slug
from ingest.slug_resolver import resolve_item_query, ResolvedQuery
from ingest.fetch_wfm import fetch_item_statistics
from ingest.fetch_vault_patch_data import (
    fetch_warframe_vault_patch_data,
    fetch_live_patchlogs,
    insert_patchlogs,
)
from nodes.graph import create_advisor_graph

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "db", "wfm.db"))

# Staleness thresholds (independent per data type)
PRICE_STALENESS_THRESHOLD = timedelta(hours=24)
VAULT_STALENESS_THRESHOLD = timedelta(days=7)
PATCH_STALENESS_THRESHOLD = timedelta(hours=24)


def init_cache_table(conn: sqlite3.Connection) -> None:
    """
    Ensures the cache_metadata table exists in the database.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_metadata (
            slug TEXT PRIMARY KEY,
            price_last_updated TIMESTAMP,
            vault_last_updated TIMESTAMP,
            patch_last_updated TIMESTAMP
        )
        """
    )
    conn.commit()


def parse_timestamp(ts: Any) -> Optional[datetime]:
    """
    Converts stored timestamp (ISO string, int, float, or datetime) to a UTC datetime object.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(ts, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(ts, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    return None


def is_stale(last_updated: Any, max_age: timedelta) -> bool:
    """
    Checks if a last_updated timestamp is missing or older than max_age.
    """
    dt = parse_timestamp(last_updated)
    if dt is None:
        return True
    now = datetime.now(tz=timezone.utc)
    return (now - dt) > max_age


def get_cache_metadata(slug: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Fetches the cache_metadata row for a slug.
    Returns dict with keys: slug, price_last_updated, vault_last_updated, patch_last_updated.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT slug, price_last_updated, vault_last_updated, patch_last_updated FROM cache_metadata WHERE slug = ?",
        (slug,),
    )
    row = cursor.fetchone()
    if row:
        return {
            "slug": row[0],
            "price_last_updated": row[1],
            "vault_last_updated": row[2],
            "patch_last_updated": row[3],
        }
    return {
        "slug": slug,
        "price_last_updated": None,
        "vault_last_updated": None,
        "patch_last_updated": None,
    }


def update_cache_timestamp(
    slug: str,
    field_name: str,
    conn: sqlite3.Connection,
    timestamp: Optional[datetime] = None,
) -> None:
    """
    Updates the specified field in cache_metadata for slug with the provided or current UTC timestamp.
    """
    ts_str = (timestamp or datetime.now(tz=timezone.utc)).isoformat()
    cursor = conn.cursor()

    if field_name == "price":
        cursor.execute(
            """
            INSERT INTO cache_metadata (slug, price_last_updated, vault_last_updated, patch_last_updated)
            VALUES (?, ?, NULL, NULL)
            ON CONFLICT(slug) DO UPDATE SET price_last_updated = excluded.price_last_updated
            """,
            (slug, ts_str),
        )
    elif field_name == "vault":
        cursor.execute(
            """
            INSERT INTO cache_metadata (slug, price_last_updated, vault_last_updated, patch_last_updated)
            VALUES (?, NULL, ?, NULL)
            ON CONFLICT(slug) DO UPDATE SET vault_last_updated = excluded.vault_last_updated
            """,
            (slug, ts_str),
        )
    elif field_name == "patch":
        cursor.execute(
            """
            INSERT INTO cache_metadata (slug, price_last_updated, vault_last_updated, patch_last_updated)
            VALUES (?, NULL, NULL, ?)
            ON CONFLICT(slug) DO UPDATE SET patch_last_updated = excluded.patch_last_updated
            """,
            (slug, ts_str),
        )
    conn.commit()


def ensure_item_record(slug: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Ensures that an item row exists in the `items` table for `slug`.
    Returns the item dictionary.
    """
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM items WHERE url_slug = ?", (slug,))
    row = cursor.fetchone()
    if row:
        return dict(row)

    frame_name, component_type = parse_slug(slug)
    if not frame_name:
        frame_name = "Unknown Frame"
        component_type = "unknown"

    item_name = slug.replace("_", " ").title()
    item_id = slug  # Fallback to slug as item_id if not pre-populated

    cursor.execute(
        """
        INSERT OR IGNORE INTO items 
        (item_id, url_slug, item_name, frame_name, component_type, vault_status, vault_date, estimated_vault_date)
        VALUES (?, ?, ?, ?, ?, 'unvaulted', NULL, NULL)
        """,
        (item_id, slug, item_name, frame_name, component_type),
    )
    conn.commit()

    cursor.execute("SELECT * FROM items WHERE url_slug = ?", (slug,))
    return dict(cursor.fetchone())


def _refresh_price_for_slug(slug: str, item_id: str, conn: sqlite3.Connection) -> bool:
    """
    Fetches fresh price history for a single slug and updates SQLite.
    Returns True on success, False on failure.
    """
    try:
        stats = fetch_item_statistics(slug)
        cursor = conn.cursor()

        for stat_window, stat_key in [("90day", "price_history_90day"), ("48hr", "price_history_48hr")]:
            for stat in stats.get(stat_key, []):
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO price_history
                    (item_id, recorded_at, avg_price, median_price, volume, moving_avg, stat_window)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        stat["timestamp"],
                        stat["avg_price"],
                        stat["median_price"],
                        stat["volume"],
                        stat["moving_avg"],
                        stat_window,
                    ),
                )
        conn.commit()
        update_cache_timestamp(slug, "price", conn)
        return True
    except Exception as e:
        logger.warning(
            f"Failed to fetch live price statistics for slug '{slug}': {e}. Using existing cached data."
        )
        return False


def _refresh_vault_for_frame(frame_name: str, frame_slugs: List[str], conn: sqlite3.Connection) -> bool:
    """
    Fetches live vault status and Prime Resurgence data for a frame and updates SQLite.
    Returns True on success, False on failure.
    """
    try:
        from ingest.fetch_vault_patch_data import fetch_prime_resurgence_data

        vault_data = fetch_warframe_vault_patch_data()
        resurgence_data = fetch_prime_resurgence_data(vault_data)
        cursor = conn.cursor()

        if frame_name in vault_data:
            info = vault_data[frame_name]
            resurg_info = resurgence_data.get(frame_name, {})

            is_active = resurg_info.get("is_active", False)
            resurg_end = resurg_info.get("resurgence_end_date")
            last_resurg_end = resurg_info.get("last_resurgence_end")

            if is_active:
                vault_status = "unvaulted"
                estimated_vault_date = resurg_end or info.get("estimatedVaultDate")
            else:
                vault_status = "vaulted" if info.get("vaulted") else "unvaulted"
                estimated_vault_date = info.get("estimatedVaultDate")

            vault_date = info.get("vaultDate")

            cursor.execute(
                """
                UPDATE items
                SET vault_status = ?,
                    vault_date = ?,
                    estimated_vault_date = ?,
                    last_resurgence_end = ?,
                    is_resurgence_active = ?,
                    resurgence_end_date = ?
                WHERE frame_name = ?
                """,
                (
                    vault_status,
                    vault_date,
                    estimated_vault_date,
                    last_resurg_end,
                    1 if is_active else 0,
                    resurg_end,
                    frame_name,
                ),
            )
            conn.commit()

        for slug in frame_slugs:
            update_cache_timestamp(slug, "vault", conn)
        return True
    except Exception as e:
        logger.warning(
            f"Failed to fetch live vault status for frame '{frame_name}': {e}. Using existing cached data."
        )
        return False


def _refresh_patch_for_frame(frame_name: str, frame_slugs: List[str], conn: sqlite3.Connection) -> bool:
    """
    Fetches live patchlogs for a frame and inserts new entries into SQLite.
    Returns True on success, False on failure.
    """
    try:
        live_patchlogs = fetch_live_patchlogs()
        insert_patchlogs(conn, [frame_name], live_patchlogs)

        for slug in frame_slugs:
            update_cache_timestamp(slug, "patch", conn)
        return True
    except Exception as e:
        logger.warning(
            f"Failed to fetch live patchlogs for frame '{frame_name}': {e}. Using existing cached data."
        )
        return False


def ensure_fresh_data(slugs: List[str], db_path: str = DB_PATH) -> None:
    """
    For each slug:
      - Looks up its cache_metadata row (or treats as all-stale if missing)
      - For each of [price, vault, patch] independently: if missing/stale, calls
        the corresponding fetch function
      - After a successful refetch, updates the cache_metadata row with current timestamp
      - Skips refetching any field that is still fresh
      - Logs errors gracefully without raising exceptions on network/API failure
    """
    if not slugs:
        return

    conn = sqlite3.connect(db_path)
    try:
        init_cache_table(conn)

        # 1. Ensure item records and group slugs by frame_name
        frame_to_slugs: Dict[str, List[str]] = {}
        slug_to_item: Dict[str, Dict[str, Any]] = {}

        for slug in slugs:
            item = ensure_item_record(slug, conn)
            slug_to_item[slug] = item
            frame_name = item.get("frame_name") or parse_slug(slug)[0] or "Unknown Frame"
            frame_to_slugs.setdefault(frame_name, []).append(slug)

        # 2. Check and refresh vault data per frame
        for frame_name, f_slugs in frame_to_slugs.items():
            # If any slug for this frame has stale vault data, refresh for the frame
            vault_stale = any(
                is_stale(get_cache_metadata(s, conn).get("vault_last_updated"), VAULT_STALENESS_THRESHOLD)
                for s in f_slugs
            )
            if vault_stale:
                logger.info(f"Vault data stale for frame '{frame_name}' — refetching...")
                _refresh_vault_for_frame(frame_name, f_slugs, conn)
            else:
                logger.debug(f"Vault data fresh for frame '{frame_name}' — skipping refetch.")

        # 3. Check and refresh patch data per frame
        for frame_name, f_slugs in frame_to_slugs.items():
            patch_stale = any(
                is_stale(get_cache_metadata(s, conn).get("patch_last_updated"), PATCH_STALENESS_THRESHOLD)
                for s in f_slugs
            )
            if patch_stale:
                logger.info(f"Patch data stale for frame '{frame_name}' — refetching...")
                _refresh_patch_for_frame(frame_name, f_slugs, conn)
            else:
                logger.debug(f"Patch data fresh for frame '{frame_name}' — skipping refetch.")

        # 4. Check and refresh price data per slug
        for slug in slugs:
            meta = get_cache_metadata(slug, conn)
            if is_stale(meta.get("price_last_updated"), PRICE_STALENESS_THRESHOLD):
                logger.info(f"Price data stale for slug '{slug}' — refetching...")
                item = slug_to_item[slug]
                _refresh_price_for_slug(slug, item["item_id"], conn)
            else:
                logger.debug(f"Price data fresh for slug '{slug}' — skipping refetch.")

    finally:
        conn.close()


def format_signal_summary(
    trend_signal: Optional[Dict[str, Any]],
    vault_signal: Optional[Dict[str, Any]],
    patch_signal: Optional[Dict[str, Any]],
) -> Dict[str, str]:
    """
    Builds a clean, concise bulleted summary of the inputs.
    """
    t_sig = trend_signal or {}
    v_sig = vault_signal or {}
    p_sig = patch_signal or {}

    # 1. Trend Summary
    pct = t_sig.get("pct_change_90d")
    r2 = t_sig.get("r_squared")
    if pct is not None:
        sign = "+" if pct > 0 else ""
        r2_str = f" (R² = {r2:.2f})" if r2 is not None else ""
        trend_summary = f"{sign}{pct:.1f}%{r2_str}"
    else:
        trend_summary = "Insufficient Data"

    # 2. Vault Summary
    v_state = v_sig.get("signal", "unknown")
    days_until = v_sig.get("days_until_vault")
    days_since = v_sig.get("days_since_vaulted")
    if v_sig.get("is_resurgence_active"):
        vault_summary = f"Resurgence Active ({days_until}d remaining)"
    elif v_state == "recently_vaulted":
        vault_summary = f"Recently Vaulted ({days_since}d ago)"
    elif v_state == "long_vaulted":
        vault_summary = f"Long Vaulted ({days_since}d ago)"
    elif v_state == "vaulting_soon":
        vault_summary = f"Vaulting Soon (~{days_until}d to vault)"
    elif v_state == "not_vaulted":
        vault_summary = f"Active ({days_until}d to vault)" if days_until else "Active (Unvaulted)"
    else:
        vault_summary = "Unknown"

    # 3. Patch Summary
    impact = p_sig.get("expected_impact", "none").capitalize()
    p_name = p_sig.get("patch_name")
    if p_name and impact != "None":
        patch_summary = f"{impact} ({p_name})"
    else:
        patch_summary = impact

    # 4. Price Summary
    curr_price = t_sig.get("current_price")
    mean_price = t_sig.get("mean_price")
    if curr_price is not None:
        curr_str = f"{int(curr_price) if curr_price == int(curr_price) else curr_price}p"
        if mean_price is not None:
            price_summary = f"{curr_str} (88d Avg: {mean_price:.1f}p)"
        else:
            price_summary = curr_str
    else:
        price_summary = "N/A"

    return {
        "price": price_summary,
        "trend": trend_summary,
        "vault": vault_summary,
        "patch": patch_summary,
    }


def format_recommendation_card(rec: Dict[str, Any]) -> str:
    """
    Formats a single recommendation dictionary into a clean, scannable terminal card.
    """
    item_name = rec.get("item_name", "Item")
    slug = rec.get("slug", "")
    sig_sum = rec.get("signal_summary") or format_signal_summary(
        rec.get("trend_signal"), rec.get("vault_signal"), rec.get("patch_signal")
    )

    lines = [
        "=" * 80,
        f"ITEM: {item_name} ({slug})",
        f"CURRENT PRICE: {sig_sum.get('price', 'N/A')}",
        "-" * 80,
        "SIGNALS:",
        f"  • Trend:        {sig_sum.get('trend', 'N/A')}",
        f"  • Vault Status: {sig_sum.get('vault', 'N/A')}",
        f"  • Patch Impact: {sig_sum.get('patch', 'N/A')}",
        f"ACTION: {rec.get('recommendation', 'HOLD')}",
        "-" * 80,
        "REASONING:",
        rec.get("reasoning", ""),
        "=" * 80,
    ]
    return "\n".join(lines)


def get_recommendation(user_input: str, db_path: str = DB_PATH) -> Dict[str, Any]:
    """
    Entry point for on-demand query architecture:
      1. Calls resolve_item_query(user_input)
      2. If ambiguous or not_found, returns immediately without touching cache or graph
      3. If resolved, calls ensure_fresh_data(slugs)
      4. Invokes the existing LangGraph pipeline per slug
      5. Returns structured recommendation results with explicit numerical breakdowns
    """
    resolved: ResolvedQuery = resolve_item_query(user_input)

    if resolved.status in ("ambiguous", "not_found"):
        return {
            "status": resolved.status,
            "query": user_input,
            "candidates": resolved.candidates,
            "component": resolved.component,
        }

    # Status is 'resolved'
    ensure_fresh_data(resolved.slugs, db_path=db_path)

    app = create_advisor_graph()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    slug_results = []
    try:
        for slug in resolved.slugs:
            cur.execute(
                "SELECT item_id, url_slug, item_name, frame_name, component_type FROM items WHERE url_slug = ?",
                (slug,),
            )
            row = cur.fetchone()
            if not row:
                continue

            item = dict(row)
            initial_state = {
                "item_id": item["item_id"],
                "url_slug": item["url_slug"],
                "item_name": item["item_name"],
                "frame_name": item["frame_name"],
                "component_type": item["component_type"],
            }

            final_state = app.invoke(initial_state)
            trend_sig = final_state.get("trend_signal") or {}
            vault_sig = final_state.get("vault_signal") or {}
            patch_sig = final_state.get("patch_signal") or {}
            sig_sum = format_signal_summary(trend_sig, vault_sig, patch_sig)

            item_rec = {
                "slug": slug,
                "item_name": item["item_name"],
                "component_type": item["component_type"],
                "recommendation": final_state.get("recommendation", "HOLD"),
                "reasoning": final_state.get("reasoning", ""),
                "trend_signal": trend_sig,
                "vault_signal": vault_sig,
                "patch_signal": patch_sig,
                "signal_summary": sig_sum,
            }
            item_rec["formatted_card"] = format_recommendation_card(item_rec)
            slug_results.append(item_rec)
    finally:
        conn.close()

    if len(slug_results) == 1:
        single = slug_results[0]
        return {
            "status": "resolved",
            "query": user_input,
            "frame_name": resolved.frame_name,
            "component": resolved.component,
            "slug": single["slug"],
            "item_name": single["item_name"],
            "component_type": single["component_type"],
            "recommendation": single["recommendation"],
            "reasoning": single["reasoning"],
            "trend_signal": single["trend_signal"],
            "vault_signal": single["vault_signal"],
            "patch_signal": single["patch_signal"],
            "signal_summary": single["signal_summary"],
            "formatted_card": single["formatted_card"],
            "results": slug_results,
        }

    return {
        "status": "resolved",
        "query": user_input,
        "frame_name": resolved.frame_name,
        "component": resolved.component,
        "slugs": resolved.slugs,
        "results": slug_results,
        "formatted_card": "\n\n".join(r["formatted_card"] for r in slug_results),
    }
