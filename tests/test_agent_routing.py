"""
tests/test_agent_routing.py — Tests for Phase 2 query classification and agent routing.
"""

import pytest
from unittest.mock import patch, MagicMock
from ingest.cache_manager import _is_conversational_query, get_recommendation


def test_is_conversational_query_detection():
    """Verify that intent keywords and conversational phrasing correctly trigger agent mode."""
    # Conversational / intent queries -> True
    assert _is_conversational_query("Is 45p fair for Rhino Prime Blueprint?") is True
    assert _is_conversational_query("Should I buy Loki Prime right now?") is True
    assert _is_conversational_query("Compare Rhino Prime vs Saryn Prime") is True
    assert _is_conversational_query("Is Volt Prime currently vaulted?") is True
    assert _is_conversational_query("Sell Saryn Prime as set or parts?") is True
    assert _is_conversational_query("What changed for Wisp Prime in recent patches?") is True
    assert _is_conversational_query("Is Gauss Prime trending up or down?") is True

    # Bare item queries -> False (fast-path pipeline)
    assert _is_conversational_query("rhino prime") is False
    assert _is_conversational_query("rhino prime bp") is False
    assert _is_conversational_query("soma prime barrel") is False
    assert _is_conversational_query("excal p sys") is False


def test_conversational_query_routes_to_agent():
    """Verify that get_recommendation dispatches conversational queries to run_market_agent."""
    with patch("ingest.cache_manager._get_run_market_agent") as mock_get_agent:
        mock_run_agent = MagicMock()
        mock_run_agent.return_value = {
            "status": "ok",
            "query": "Is 45p fair for Rhino Prime Blueprint?",
            "response": "45p is a fair price.",
            "messages": [],
        }
        mock_get_agent.return_value = mock_run_agent

        res = get_recommendation("Is 45p fair for Rhino Prime Blueprint?")

        assert res.get("agent_mode") is True
        assert res.get("status") == "ok"
        assert "45p is a fair price." in res.get("response")
        mock_run_agent.assert_called_once()


def test_batch_threshold_and_resurgence_alerts():
    """Verify that run_batch_evaluation generates resurgence countdown and threshold alerts."""
    from lambda_handler import run_batch_evaluation

    mock_rec = {
        "status": "resolved",
        "results": [{
            "item_name": "Test Prime Set",
            "recommendation": "HOLD",
            "trend_signal": {"current_price": 105.0, "pct_change_90d": 5.0, "confidence": "high"},
            "vault_signal": {"is_resurgence_active": True, "days_until_vault": 4},
        }]
    }

    mock_watchlist = [
        {"name": "Test Prime", "target_sell_price": 100, "target_buy_price": 50}
    ]

    with patch("lambda_handler.WATCHLIST_PATH", "/nonexistent/path"), \
         patch("lambda_handler.open"), \
         patch("json.load", return_value=mock_watchlist), \
         patch("os.path.exists", return_value=True), \
         patch("lambda_handler.get_recommendation", return_value=mock_rec), \
         patch("lambda_handler.send_discord_alert") as mock_discord, \
         patch("lambda_handler.sync_db_to_s3"):

        summary = run_batch_evaluation()

        assert summary["status"] == "success"
        # Resurgence alert triggered because days_until_vault <= 7
        assert len(summary["resurgence_alerts"]) == 1
        assert summary["resurgence_alerts"][0]["days_remaining"] == 4

        # Threshold alert triggered because current_price (105) >= target_sell_price (100)
        assert len(summary["threshold_alerts"]) == 1
        assert summary["threshold_alerts"][0]["type"] == "sell"
        assert summary["threshold_alerts"][0]["current_price"] == 105.0

        mock_discord.assert_called_once()
