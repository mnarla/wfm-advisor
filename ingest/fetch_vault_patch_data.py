import os
import sqlite3
import json
import logging
import re
import requests
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

WARFRAMES_JSON_URL = "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Warframes.json"
WEAPON_JSON_URLS = [
    "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Primary.json",
    "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Secondary.json",
    "https://raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Melee.json",
]
PATCHLOGS_JSON_URL = "https://raw.githubusercontent.com/WFCD/warframe-patchlogs/master/data/patchlogs.json"
VAULT_TRADER_URL = "https://api.warframestat.us/pc/vaultTrader"
DB_PATH = os.getenv("DB_PATH", "db/wfm.db")
WATCHLIST_PATH = "config/watchlist.json"


def fetch_warframe_vault_patch_data() -> Dict[str, Any]:
    """
    GETs the WFCD Warframes.json and weapon (Primary/Secondary/Melee) JSON datasets
    and returns a unified dict keyed by item name (e.g. 'Saryn Prime', 'Soma Prime')
    for O(1) lookup. Each value contains:
      - vaulted: bool
      - vaultDate: str | None
      - estimatedVaultDate: str | None
      - releaseDate: str | None
      - category: str ('warframe' | 'primary' | 'secondary' | 'melee')
    """
    data = {}

    # Load warframes
    logger.info(f"Fetching WFCD Warframes dataset from {WARFRAMES_JSON_URL}...")
    response = requests.get(WARFRAMES_JSON_URL, timeout=30)
    response.raise_for_status()
    for entry in response.json():
        name = entry.get("name", "").strip()
        if not name:
            continue
        data[name] = {
            "vaulted": entry.get("vaulted", False),
            "vaultDate": entry.get("vaultDate"),
            "estimatedVaultDate": entry.get("estimatedVaultDate"),
            "releaseDate": entry.get("releaseDate"),
            "category": "warframe",
        }
    logger.info(f"Loaded {len(data)} Warframe entries from WFCD dataset.")

    # Load Prime weapons (Primary, Secondary, Melee)
    weapon_count = 0
    category_names = ["primary", "secondary", "melee"]
    for weapon_url, cat_name in zip(WEAPON_JSON_URLS, category_names):
        try:
            resp = requests.get(weapon_url, timeout=30)
            resp.raise_for_status()
            for entry in resp.json():
                if not entry.get("isPrime"):
                    continue
                name = entry.get("name", "").strip()
                if not name:
                    continue
                data[name] = {
                    "vaulted": entry.get("vaulted", False),
                    "vaultDate": entry.get("vaultDate"),
                    "estimatedVaultDate": entry.get("estimatedVaultDate"),
                    "releaseDate": entry.get("releaseDate"),
                    "category": cat_name,
                }
                weapon_count += 1
        except Exception as e:
            logger.warning(f"Failed to fetch weapon vault data from {weapon_url}: {e}")
    logger.info(f"Loaded {weapon_count} Prime weapon entries from WFCD dataset.")

    return data


def build_prime_access_groups(vault_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Builds a weapon→frame mapping using shared releaseDate from WFCD data.

    Prime Access waves release a frame and its companion weapons on the same date.
    This function groups all items by releaseDate, identifies the frame in each group,
    and maps each weapon in that group to its companion frame.

    Returns: Dict mapping weapon name → companion frame name.
             e.g. {'Soma Prime': 'Nova Prime', 'Vasto Prime': 'Nova Prime'}
    """
    # Group by releaseDate
    release_groups: Dict[str, List[tuple]] = {}  # date -> [(name, category)]
    for name, info in vault_data.items():
        rel_date = info.get("releaseDate")
        if not rel_date or "Prime" not in name:
            continue
        rel_key = rel_date[:10]  # Normalize to YYYY-MM-DD
        if rel_key not in release_groups:
            release_groups[rel_key] = []
        release_groups[rel_key].append((name, info.get("category", "")))

    # Build weapon → frame mapping from groups that have exactly one frame
    weapon_to_frame: Dict[str, str] = {}
    for date, items in release_groups.items():
        frames = [(n, c) for n, c in items if c == "warframe"]
        weapons = [(n, c) for n, c in items if c != "warframe"]

        if len(frames) != 1:
            # Skip groups with 0 or 2+ frames — can't determine association
            if frames and weapons:
                logger.debug(
                    f"Skipping ambiguous Prime Access group at {date}: "
                    f"frames={[n for n, _ in frames]}, weapons={[n for n, _ in weapons]}"
                )
            continue

        frame_name = frames[0][0]
        for weapon_name, _ in weapons:
            weapon_to_frame[weapon_name] = frame_name

    logger.info(
        f"Built Prime Access grouping: {len(weapon_to_frame)} weapons mapped to companion frames."
    )
    return weapon_to_frame


def fetch_prime_resurgence_data(vault_data: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """
    GETs live Prime Resurgence data from the Warframe WorldState API (vaultTrader).
    Returns a dict keyed by item name (frames AND weapons) with:
      - is_active: bool (whether currently unvaulted in Varzia's active rotation)
      - resurgence_end_date: str | None (current rotation expiry ISO timestamp)
      - last_resurgence_end: str | None (ISO date of most recent ended resurgence)
      - pack_name: str | None
      - confidence: str ('direct' for frames matched from schedule, 'inherited' for
        weapons inferred via Prime Access release-date grouping)

    If vault_data is provided, weapon resurgence entries are propagated from their
    companion frame using the releaseDate-based Prime Access grouping.
    """

    try:
        response = requests.get(
            VAULT_TRADER_URL,
            headers={"User-Agent": "wfm-sell-timing-advisor/1.0", "Accept": "application/json"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning(f"Failed to fetch Prime Resurgence data from {VAULT_TRADER_URL}: {e}")
        return {}

    now = datetime.now(tz=timezone.utc)
    active_inventory = [item.get("item", "") for item in data.get("inventory", [])]
    current_expiry = data.get("expiry")
    schedule = data.get("schedule", [])

    resurgence_map: Dict[str, Dict[str, Any]] = {}

    # Helper to check if a frame is in active inventory
    def is_in_active_inventory(frame_name: str) -> bool:
        base = frame_name.replace(" Prime", "").lower()
        return any(base in inv.lower() for inv in active_inventory)

    # Process schedule history for all past rotations
    for entry in schedule:
        item_str = entry.get("item", "")
        expiry_str = entry.get("expiry")
        if not expiry_str:
            continue
        try:
            expiry_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        except Exception:
            continue

        # Extract words that could represent prime frames
        for word in re.findall(r"[A-Za-z]+", item_str):
            if word.lower() in (
                "prime", "m", "p", "v", "dual", "single", "pack", "armor",
                "set", "weapon", "item", "last", "chance", "a", "b", "c"
            ):
                continue
            frame_candidate = f"{word.capitalize()} Prime"

            if frame_candidate not in resurgence_map or expiry_dt > resurgence_map[frame_candidate]["last_resurgence_end_dt"]:
                resurgence_map[frame_candidate] = {
                    "last_resurgence_end_dt": expiry_dt,
                    "last_resurgence_end": expiry_str[:10],  # YYYY-MM-DD
                    "pack_name": item_str,
                }

    # Finalize records with active status and expiry
    result: Dict[str, Dict[str, Any]] = {}
    for frame_name, info in resurgence_map.items():
        is_active = is_in_active_inventory(frame_name)
        result[frame_name] = {
            "is_active": is_active,
            "resurgence_end_date": current_expiry[:10] if (is_active and current_expiry) else None,
            "last_resurgence_end": info["last_resurgence_end"],
            "pack_name": info["pack_name"],
            "confidence": "direct",
        }

    # Also check if any frame in active inventory wasn't in past schedule history
    for inv_item in active_inventory:
        for word in re.findall(r"[A-Za-z]+", inv_item):
            if word.lower() in ("prime", "m", "p", "v", "dual", "single", "pack", "armor", "set", "weapon", "item"):
                continue
            frame_candidate = f"{word.capitalize()} Prime"
            if frame_candidate not in result:
                result[frame_candidate] = {
                    "is_active": True,
                    "resurgence_end_date": current_expiry[:10] if current_expiry else None,
                    "last_resurgence_end": None,
                    "pack_name": inv_item,
                    "confidence": "direct",
                }
            else:
                result[frame_candidate]["is_active"] = True
                result[frame_candidate]["resurgence_end_date"] = current_expiry[:10] if current_expiry else None

    # Propagate resurgence data to companion weapons via Prime Access grouping
    # Weapons vault/unvault as part of the same relic cycle as their companion frame,
    # so they inherit the same resurgence schedule.
    if vault_data:
        weapon_to_frame = build_prime_access_groups(vault_data)
        weapons_propagated = 0
        for weapon_name, companion_frame in weapon_to_frame.items():
            if companion_frame in result and weapon_name not in result:
                frame_entry = result[companion_frame]
                result[weapon_name] = {
                    "is_active": frame_entry["is_active"],
                    "resurgence_end_date": frame_entry["resurgence_end_date"],
                    "last_resurgence_end": frame_entry["last_resurgence_end"],
                    "pack_name": frame_entry["pack_name"],
                    "confidence": "inherited",
                }
                weapons_propagated += 1
        logger.info(
            f"Propagated resurgence data to {weapons_propagated} companion weapons."
        )

    logger.info(f"Loaded Prime Resurgence data for {len(result)} items (frames + weapons).")
    return result


def fetch_live_patchlogs() -> List[Dict[str, Any]]:
    """
    GETs the raw patchlogs.json URL from @wfcd/patchlogs repo.
    Returns the full list of raw patchlogs.
    """
    logger.info(f"Fetching live patchlogs from {PATCHLOGS_JSON_URL}...")
    response = requests.get(PATCHLOGS_JSON_URL, timeout=30)
    response.raise_for_status()
    raw_patchlogs = response.json()
    logger.info(f"Loaded {len(raw_patchlogs)} total patchlogs from live source.")
    return raw_patchlogs


def match_patchlogs_to_frame(all_patchlogs: List[Dict[str, Any]], frame_name: str) -> List[Dict[str, Any]]:
    """
    Filters all patchlogs to entries where frame_name (or its base name without 'Prime')
    appears in name/additions/changes/fixes.
    """
    matched = []
    base_name = frame_name.replace(" Prime", "").strip()
    base_lower = base_name.lower()
    frame_lower = frame_name.lower()

    for p in all_patchlogs:
        name_txt = p.get("name") or ""
        add_txt = p.get("additions") or ""
        chg_txt = p.get("changes") or ""
        fix_txt = p.get("fixes") or ""

        combined_txt = f"{name_txt}\n{add_txt}\n{chg_txt}\n{fix_txt}".lower()
        if frame_lower in combined_txt or base_lower in combined_txt:
            matched.append(p)

    return matched


def update_items_vault_status(
    conn: sqlite3.Connection,
    watchlist: list,
    source_data: Dict[str, Any],
    resurgence_data: Optional[Dict[str, Any]] = None,
) -> int:
    """
    For each frame in the watchlist, looks up its vault info in source_data and
    Prime Resurgence info, and UPDATEs component rows in `items` WHERE frame_name = ?.
    """
    cursor = conn.cursor()
    total_updated = 0
    resurgence_map = resurgence_data or {}

    for frame_name in watchlist:
        if frame_name not in source_data:
            logger.warning(f"Frame '{frame_name}' not found in WFCD dataset — skipping vault status update.")
            continue

        info = source_data[frame_name]
        resurg_info = resurgence_map.get(frame_name, {})

        is_active = resurg_info.get("is_active", False)
        resurg_end = resurg_info.get("resurgence_end_date")
        last_resurg_end = resurg_info.get("last_resurgence_end")

        if is_active:
            vault_status = "unvaulted"
            estimated_vault_date = resurg_end or info["estimatedVaultDate"]
        else:
            vault_status = "vaulted" if info["vaulted"] else "unvaulted"
            estimated_vault_date = info["estimatedVaultDate"]

        vault_date = info["vaultDate"]

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
        total_updated += cursor.rowcount

    conn.commit()
    return total_updated


def insert_patchlogs(conn: sqlite3.Connection, watchlist: list, all_patchlogs: List[Dict[str, Any]]) -> int:
    """
    Filters and inserts matched patchlogs for each frame on the watchlist.
    De-duplicates by checking (frame_name, patch_name) pairs.
    """
    cursor = conn.cursor()

    # Load existing (frame_name, patch_name) pairs to avoid duplicates
    cursor.execute("SELECT frame_name, patch_name FROM patchlogs")
    existing = set(cursor.fetchall())

    total_inserted = 0

    for frame_name in watchlist:
        matched_logs = match_patchlogs_to_frame(all_patchlogs, frame_name)
        logger.info(f"Matched {len(matched_logs)} patchlogs for frame '{frame_name}'")

        for patch in matched_logs:
            patch_name = patch.get("name", "")
            patch_date = patch.get("date", "")

            if not patch_name or not patch_date:
                continue

            if (frame_name, patch_name) in existing:
                continue

            cursor.execute(
                """
                INSERT INTO patchlogs (frame_name, patch_name, patch_date, patch_url, additions, changes, fixes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    frame_name,
                    patch_name,
                    patch_date,
                    patch.get("url"),
                    patch.get("additions"),
                    patch.get("changes"),
                    patch.get("fixes"),
                ),
            )
            existing.add((frame_name, patch_name))
            total_inserted += 1

    conn.commit()
    return total_inserted


def main():
    """
    Orchestrates the ingestion pipeline.
    """
    with open(WATCHLIST_PATH) as f:
        watchlist = json.load(f)

    # 1. Fetch source data
    vault_data = fetch_warframe_vault_patch_data()
    resurgence_data = fetch_prime_resurgence_data(vault_data)
    live_patchlogs = fetch_live_patchlogs()

    # 2. Update DB
    conn = sqlite3.connect(DB_PATH)
    try:
        items_updated = update_items_vault_status(conn, watchlist, vault_data, resurgence_data)
        patchlogs_inserted = insert_patchlogs(conn, watchlist, live_patchlogs)
    finally:
        conn.close()

    print(f"\n=== Vault, Prime Resurgence & Live Patch Ingestion Summary ===")
    print(f"  Items rows updated (vault & resurgence status): {items_updated}")
    print(f"  Patchlog rows inserted:                         {patchlogs_inserted}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
