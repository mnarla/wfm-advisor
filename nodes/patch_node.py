"""
nodes/patch_node.py — Patch-context reasoning node for the LangGraph sell-timing pipeline.

WHY THIS NODE EXISTS:
Price trends alone don't explain WHY something is moving — a rising price could be
due to a buff making the frame more desirable, or could be unrelated market noise.
This node adds semantic context: does a real game-balance change explain the trend,
or is the trend unexplained by anything in recent patches? This is real reasoning
work, not just data lookup — it's why an LLM is used here specifically, unlike
vault_node and trend_node which are pure code.

Most patchlog entries are irrelevant noise (cosmetic fixes, unrelated bug fixes for
other parts of the game bundled in the same patch). The LLM distinguishes signal
from noise, avoiding false positives where weak/cosmetic entries are stretched to
claim relevance.
"""

import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "db/wfm.db")
PATCHLOG_LOOKBACK_DAYS = 90  # Match trend_node's 90-day window


def get_recent_patchlogs(frame_name: str, conn: sqlite3.Connection, days: int = PATCHLOG_LOOKBACK_DAYS) -> List[Dict[str, Any]]:
    """
    Pure DB query: fetches patchlogs for a given frame_name within the last `days` days.
    Returns a list of dicts with keys: patch_name, patch_date, patch_url, additions, changes, fixes.
    """
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT patch_name, patch_date, patch_url, additions, changes, fixes
        FROM patchlogs
        WHERE frame_name = ? AND patch_date >= ?
        ORDER BY patch_date DESC
        """,
        (frame_name, cutoff),
    )
    return [dict(row) for row in cur.fetchall()]


def _extract_relevant_patch_lines(text: Optional[str], frame_name: str) -> str:
    """Extracts bullet points and lines containing the frame or base name to avoid token bloat."""
    if not text:
        return ""
    base = frame_name.replace(" Prime", "").strip().lower()
    fn = frame_name.lower()
    matched = [line.strip() for line in text.splitlines() if line.strip() and (fn in line.lower() or base in line.lower())]
    return "\n".join(matched) if matched else text[:500]


def build_patch_context_prompt(frame_name: str, patchlogs: List[Dict[str, Any]]) -> str:
    """
    Constructs the LLM prompt for patch-context analysis. Extracts only relevant
    lines mentioning the item to keep prompt concise and within token budgets.
    """
    patch_entries = []
    for i, p in enumerate(patchlogs, 1):
        entry_parts = [f"Patch #{i}: {p['patch_name']} ({p['patch_date']})"]
        
        adds = _extract_relevant_patch_lines(p.get("additions"), frame_name)
        chgs = _extract_relevant_patch_lines(p.get("changes"), frame_name)
        fixs = _extract_relevant_patch_lines(p.get("fixes"), frame_name)
        
        if adds:
            entry_parts.append(f"  Additions: {adds}")
        if chgs:
            entry_parts.append(f"  Changes: {chgs}")
        if fixs:
            entry_parts.append(f"  Fixes: {fixs}")
            
        patch_entries.append("\n".join(entry_parts))

    patches_text = "\n\n".join(patch_entries) if patch_entries else "(No patch entries found)"

    prompt = f"""You are analyzing Warframe patch notes to determine if any recent game changes
could meaningfully impact market prices for "{frame_name}" components on Warframe Market.

FRAME: {frame_name}

RECENT PATCH ENTRIES (last {PATCHLOG_LOOKBACK_DAYS} days):
{patches_text}

INSTRUCTIONS:
1. Only consider content that is CLEARLY and SPECIFICALLY about {frame_name} itself —
   its abilities, stats, augments, or direct gameplay changes. Ignore entries that
   mention other Warframes, weapons, or general game systems unless they directly
   affect {frame_name}'s viability or desirability.

2. Classify whether any entry represents a MEANINGFUL game-balance change:
   - BUFF: Ability damage increased, survivability improved, augment added/improved,
     synergy with meta builds enhanced → would INCREASE demand/price
   - NERF: Ability effectiveness reduced, interaction removed, meta shift away →
     would DECREASE demand/price
   - REWORK: Major ability overhaul → impact is UNCLEAR until community reception settles
   - IRRELEVANT: Cosmetic fixes, unrelated bug fixes, text corrections, other items
     mentioned in the same patch → NO expected market impact

3. Be conservative: if a patch entry is ambiguous or only tangentially related to
   {frame_name}, classify it as irrelevant. False positives (claiming relevance that
   isn't there) are WORSE than saying "no relevant patch found."

4. If NO patch entries are provided, or none are relevant, respond with
   relevant_patch_found = false.

Respond with ONLY valid JSON (no markdown fences, no explanation outside the JSON):
{{
  "relevant_patch_found": true/false,
  "patch_name": "name of the relevant patch" or null,
  "expected_impact": "increase" or "decrease" or "unclear" or "none",
  "reasoning": "One-sentence plain-English justification for your judgment"
}}"""

    return prompt


from nodes.synthesis_node import _call_fallback_llm


def call_llm_for_patch_analysis(prompt: str) -> Dict[str, Any]:
    """
    Calls Gemini API to analyze patch context. The actual API interaction is isolated
    here so swapping providers later (e.g. OpenRouter fallback) only requires changing
    this function, not the prompt logic or orchestration.

    Handles malformed JSON by retrying once, then falling back to a safe default.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    raw_text = None

    safe_default = {
        "relevant_patch_found": False,
        "patch_name": None,
        "expected_impact": "none",
        "reasoning": "LLM response could not be parsed.",
    }

    # If Gemini API Key is valid, try using the official google-genai client
    if api_key and api_key != "your_gemini_api_key_here":
        from google import genai
        from google.genai import types
        try:
            client = genai.Client(api_key=api_key)
            config = types.GenerateContentConfig(temperature=0.0)
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt,
                config=config,
            )
            raw_text = response.text.strip()
        except Exception as e:
            logger.warning(f"Official Gemini API call failed: {e}. Falling back...")

    # If Gemini failed or was not configured, try fallbacks
    if not raw_text:
        raw_text = _call_fallback_llm(prompt)

    if not raw_text:
        logger.error("All LLM providers failed or no API keys configured.")
        return {
            "relevant_patch_found": False,
            "patch_name": None,
            "expected_impact": "none",
            "reasoning": "LLM analysis skipped: No active API keys configured.",
        }

    for attempt in range(2):
        try:
            # Strip markdown code fences if present
            clean_text = raw_text.strip()
            clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text)
            clean_text = re.sub(r"\s*```$", "", clean_text)

            parsed = json.loads(clean_text)

            return {
                "relevant_patch_found": bool(parsed.get("relevant_patch_found", False)),
                "patch_name": parsed.get("patch_name"),
                "expected_impact": parsed.get("expected_impact", "none"),
                "reasoning": parsed.get("reasoning", "No reasoning provided."),
            }

        except json.JSONDecodeError:
            if attempt == 0:
                logger.warning(f"LLM returned malformed JSON (attempt {attempt + 1}), retrying fallback...")
                raw_text = _call_fallback_llm(prompt)
                if not raw_text:
                    return safe_default
            else:
                logger.error(f"LLM returned malformed JSON after retry. Raw: {raw_text[:200]}")
                return safe_default
        except Exception as e:
            logger.error(f"Failed to parse LLM results: {e}")
            return safe_default

    return safe_default


def compute_patch_signal(frame_name: str, conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    Orchestrates the patch analysis pipeline:
    1. get_recent_patchlogs — fetch relevant patches from DB
    2. build_patch_context_prompt — construct the LLM prompt
    3. call_llm_for_patch_analysis — get structured judgment
    Returns the full signal dict.
    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM patchlogs WHERE frame_name = ?", (frame_name,))
    total_in_db = cur.fetchone()[0]

    patchlogs = get_recent_patchlogs(frame_name, conn)

    logger.debug(f"Frame: {frame_name}")
    logger.debug(f"Total patchlogs in DB: {total_in_db}")
    logger.debug(f"Patchlogs within 90-day window: {len(patchlogs)}")
    if patchlogs:
        for p in patchlogs:
            logger.debug(f"  - Included patch: '{p.get('patch_name')}' | Date: {p.get('patch_date')}")
    else:
        logger.debug("  - No patches within 90-day window.")

    if not patchlogs:
        return {
            "relevant_patch_found": False,
            "patch_name": None,
            "expected_impact": "none",
            "reasoning": f"No patchlog entries found for {frame_name} in the last {PATCHLOG_LOOKBACK_DAYS} days.",
            "patchlogs_checked": 0,
            "total_patchlogs_in_db": total_in_db,
        }

    # Bound context window: evaluate up to the 8 most recent patch entries
    capped_patchlogs = patchlogs[:8]
    prompt = build_patch_context_prompt(frame_name, capped_patchlogs)
    result = call_llm_for_patch_analysis(prompt)
    result["patchlogs_checked"] = len(capped_patchlogs)
    result["total_patchlogs_in_db"] = total_in_db

    return result


def patch_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    LangGraph node: reads frame_name from state (or fetches from DB via item_id),
    analyzes recent patchlogs via LLM, and writes the patch_signal back to state.
    """
    frame_name = state.get("frame_name")
    item_id = state.get("item_id")

    if not frame_name and item_id:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT frame_name FROM items WHERE item_id = ?", (item_id,))
        row = cur.fetchone()
        conn.close()
        if row:
            frame_name = row["frame_name"]

    if not frame_name:
        return {
            "patch_signal": {
                "relevant_patch_found": False,
                "patch_name": None,
                "expected_impact": "none",
                "reasoning": "No frame_name in state.",
                "patchlogs_checked": 0,
            }
        }

    conn = sqlite3.connect(DB_PATH)
    try:
        signal = compute_patch_signal(frame_name, conn)
    finally:
        conn.close()

    return {"patch_signal": signal}
