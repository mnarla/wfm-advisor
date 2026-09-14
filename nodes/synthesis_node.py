"""
nodes/synthesis_node.py — Synthesis & decision node for the LangGraph sell-timing pipeline.

WHY THIS NODE EXISTS:
The trend, vault, and patch nodes each produce an independent signal about an item.
This node acts as the final decision maker: it combines all three signals, weighs
conflicting inputs (e.g. rising price vs. long-vaulted status vs. patch notes),
and synthesizes a final SELL or HOLD recommendation with plain-English justification.
"""

import os
import re
import json
import sqlite3
import logging
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from dotenv import load_dotenv
from nodes.state import AgentState

load_dotenv()

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "db/wfm.db")


def build_synthesis_prompt(
    item_name: str,
    trend_signal: Dict[str, Any],
    vault_signal: Dict[str, Any],
    patch_signal: Dict[str, Any],
) -> str:
    """
    Constructs the LLM prompt combining trend, vault, and patch signals into a structured request.
    Separated for clarity and testing.
    """
    trend_summary = trend_signal.get("reasoning", "No trend data available.")
    vault_summary = vault_signal.get("reasoning", "No vault data available.")
    patch_summary = patch_signal.get("reasoning", "No patch data available.")

    prompt = f"""You are an expert market analyst for Warframe Market (wfm-sell-timing-advisor).
Your task is to analyze three independent signals for the item "{item_name}" and provide a final recommendation: SELL or HOLD.

ITEM: {item_name}

SIGNAL 1: PRICE TREND (Statistical Analysis)
Summary: {trend_summary}
Raw Data:
- Signal Direction: {trend_signal.get('signal', 'unknown')}
- Slope (plat/day): {trend_signal.get('slope')}
- Fit Confidence (R²): {trend_signal.get('r_squared')} ({trend_signal.get('confidence', 'unknown')} confidence)
- 90-Day Percent Change: {trend_signal.get('pct_change_90d')}%
- Low Price Item Flag: {trend_signal.get('low_price_item', False)}

SIGNAL 2: VAULT STATUS (Supply & Relic Cycle)
Summary: {vault_summary}
Raw Data:
- Vault Status Signal: {vault_signal.get('signal', 'unknown')}
- Days Since Vaulted: {vault_signal.get('days_since_vaulted')}
- Days Until Vault: {vault_signal.get('days_until_vault')}
- Is Resurgence Active: {vault_signal.get('is_resurgence_active', False)}

SIGNAL 3: PATCH & BALANCE NOTES (Semantic Context)
Summary: {patch_summary}
Raw Data:
- Relevant Patch Found: {patch_signal.get('relevant_patch_found', False)}
- Patch Name: {patch_signal.get('patch_name')}
- Expected Market Impact: {patch_signal.get('expected_impact', 'none')}

DECISION HIERARCHY (Follow these rules strictly in sequential order):

STEP 1 — PATCH IMPACT (Highest Priority):
If Expected Market Impact is NOT "none" (i.e. "increase", "decrease", or "unclear" from a confirmed gameplay buff, nerf, or rework):
- "increase" (Buff) -> Recommend SELL (demand spike creates prime selling window). Primary driver: "patch".
- "decrease" (Nerf) or "unclear" (Rework) -> Recommend HOLD (avoid selling at depressed prices or wait for meta to settle). Primary driver: "patch".
- STOP here and DO NOT evaluate trend or vault status if a valid patch impact is found.

STEP 2 — TREND CONFIDENCE (Checked only if Patch Impact is "none"):
Check the statistical Fit Confidence (R²):
- If R² > 0.45 (High/Trustworthy Trend Confidence):
  * Positive Trend (% Change > 0 or Slope > 0) -> Recommend SELL (WFM price trends are scarcity-driven; an already-confirmed uptrend indicates mature gains, so lock in profit now rather than waiting). Primary driver: "trend".
  * Negative Trend (% Change < 0 or Slope < 0) -> Recommend HOLD (do not sell into a confirmed decline). Primary driver: "trend".
  * STOP here and DO NOT evaluate vault status if R² > 0.45.

STEP 3 — VAULT STATUS (Checked only if Patch Impact is "none" AND Trend R² <= 0.45):
When trend confidence is weak or noisy (R² <= 0.45), vault status is the deciding factor:
- "recently_vaulted" -> Recommend HOLD (supply was recently cut off and market is still absorbing remaining inventory; hold for long-term appreciation). Primary driver: "vault".
- "vaulting_soon" or "is_resurgence_active" -> Recommend HOLD or SELL based on relic timing. Primary driver: "vault".
- "long_vaulted" -> Recommend HOLD (price has stabilized). Primary driver: "vault".

STEP 4 — DEFAULT CONSERVATIVE FALLBACK:
If Trend R² <= 0.45 AND Vault Status is "not_vaulted" / inconclusive -> Recommend HOLD. Primary driver: "combined".

GENERAL RULES:
1. Ground your decision ONLY in the three provided signals following the hierarchy above.
2. Select a single `primary_driver` from: "trend", "vault", "patch", "combined".
3. Write 2-3 sentences of clear plain-English reasoning suitable for an end-user display explaining the decision. Avoid technical statistical jargon like "R-squared" or "linear regression".

Respond with ONLY valid JSON (no markdown fences, no text outside the JSON):
{{
  "recommendation": "SELL" or "HOLD",
  "confidence": "low" or "medium" or "high",
  "primary_driver": "trend" or "vault" or "patch" or "combined",
  "reasoning": "Plain-English explanation (2-3 sentences)"
}}"""

    return prompt


def _call_fallback_llm(prompt: str) -> Optional[str]:
    """
    Fallback LLM caller using raw requests to OpenRouter,
    matching patch_node.py's implementation. Retries on 429 rate limits.
    """
    import time
    import requests

    openrouter_key = os.getenv("OPENROUTER_API_KEY")

    if openrouter_key and openrouter_key != "your_openrouter_api_key_here":
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/mnarla/wfmarket-scout",
            "X-Title": "WFM Sell-Timing Advisor",
        }
        data = {
            "model": "openai/gpt-oss-20b:free",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 1000,
        }
        for attempt in range(2):
            try:
                logger.info("Attempting LLM call via OpenRouter fallback...")
                res = requests.post(url, json=data, headers=headers, timeout=20)
                if res.status_code == 429 and attempt == 0:
                    logger.warning("OpenRouter returned 429 rate limit. Waiting 5s before retry...")
                    time.sleep(5.0)
                    continue
                res.raise_for_status()
                return res.json()["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning(f"OpenRouter fallback failed: {e}")

    return None


def call_llm_for_synthesis(prompt: str) -> Dict[str, Any]:
    """
    Calls Gemini API (or OpenRouter fallback) to generate the synthesis decision.
    Matches patch_node.py's client structure and error handling.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    raw_text = None

    safe_default = {
        "recommendation": "HOLD",
        "confidence": "low",
        "primary_driver": "combined",
        "reasoning": "Unable to parse LLM recommendation. Defaulting to HOLD for safety.",
    }

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

    if not raw_text:
        raw_text = _call_fallback_llm(prompt)

    if not raw_text:
        logger.error("All LLM providers failed or no API keys configured.")
        return {
            "recommendation": "HOLD",
            "confidence": "low",
            "primary_driver": "combined",
            "reasoning": "LLM analysis skipped: No active API keys configured. Defaulting to HOLD.",
        }

    for attempt in range(2):
        try:
            clean_text = raw_text.strip()
            clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text)
            clean_text = re.sub(r"\s*```$", "", clean_text)

            parsed = json.loads(clean_text)

            rec = str(parsed.get("recommendation", "HOLD")).upper()
            if rec not in ("SELL", "HOLD"):
                rec = "HOLD"

            conf = str(parsed.get("confidence", "medium")).lower()
            if conf not in ("low", "medium", "high"):
                conf = "medium"

            driver = str(parsed.get("primary_driver", "combined")).lower()
            if driver not in ("trend", "vault", "patch", "combined"):
                driver = "combined"

            return {
                "recommendation": rec,
                "confidence": conf,
                "primary_driver": driver,
                "reasoning": str(parsed.get("reasoning", safe_default["reasoning"])),
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


def compute_synthesis(
    item_id: str,
    item_name: str,
    trend_signal: Dict[str, Any],
    vault_signal: Dict[str, Any],
    patch_signal: Dict[str, Any],
    conn: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    """
    Orchestrates synthesis: builds prompt, invokes LLM, and returns decision dict.
    """
    prompt = build_synthesis_prompt(item_name, trend_signal, vault_signal, patch_signal)
    return call_llm_for_synthesis(prompt)


def synthesis_node(state: AgentState) -> AgentState:
    """
    LangGraph node: reads signals from state, calls LLM for synthesis,
    and updates state with the recommendation and reasoning.
    """
    item_id = state.get("item_id", "")
    item_name = state.get("item_name", "Unknown Item")
    trend_signal = state.get("trend_signal", {})
    vault_signal = state.get("vault_signal", {})
    patch_signal = state.get("patch_signal", {})

    decision = compute_synthesis(
        item_id=item_id,
        item_name=item_name,
        trend_signal=trend_signal,
        vault_signal=vault_signal,
        patch_signal=patch_signal,
    )

    state["recommendation"] = decision["recommendation"]
    state["reasoning"] = decision["reasoning"]

    return state
