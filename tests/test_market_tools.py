"""
tests/test_market_tools.py — Unit tests for Phase 2 specialized domain tools in nodes/tools.py.
"""

import pytest
from unittest.mock import patch, MagicMock
from nodes.tools import (
    get_price_trend,
    get_vault_status,
    get_patch_impact,
    check_fair_price,
    compare_set_vs_parts,
    get_market_signals,
)


def test_get_price_trend_not_found():
    """Verify tool handles unknown items gracefully."""
    with patch("nodes.tools.resolve_item_query") as mock_resolve:
        mock_resolve.return_value = MagicMock(status="not_found", slugs=[])
        res = get_price_trend.invoke({"item_name": "nonexistent_item_xyz"})
        assert "Could not find item" in res


def test_get_vault_status_not_found():
    """Verify tool handles unknown items gracefully."""
    with patch("nodes.tools.resolve_item_query") as mock_resolve:
        mock_resolve.return_value = MagicMock(status="not_found", slugs=[])
        res = get_vault_status.invoke({"item_name": "nonexistent_item_xyz"})
        assert "Could not find item" in res


def test_get_patch_impact_not_found():
    """Verify tool handles unknown items gracefully."""
    with patch("nodes.tools.resolve_item_query") as mock_resolve:
        mock_resolve.return_value = MagicMock(status="not_found", slugs=[])
        res = get_patch_impact.invoke({"item_name": "nonexistent_item_xyz"})
        assert "Could not find item" in res


def test_check_fair_price_bargain():
    """Verify fair price tool identifies bargain prices (significantly below market)."""
    with patch("nodes.tools._resolve_and_freshen") as mock_resolve, \
         patch("nodes.tools.sqlite3.connect") as mock_conn, \
         patch("nodes.tools._fetch_item_row") as mock_row, \
         patch("nodes.tools._fetch_48hr_prices") as mock_48hr, \
         patch("nodes.tools.compute_trend_signal") as mock_trend:

        mock_resolve.return_value = ("Rhino Prime", "set", ["rhino_prime_set"])
        mock_row.return_value = {"item_id": "1", "item_name": "Rhino Prime Set", "url_slug": "rhino_prime_set"}
        mock_48hr.return_value = [{"median_price": 100.0}]
        mock_trend.return_value = {"current_price": 100.0, "mean_price": 95.0}

        # 50p is 50% below 100p market -> Bargain
        res = check_fair_price.invoke({"item_name": "Rhino Prime", "offered_price": 50.0})
        assert "BARGAIN" in res
        assert "50p" in res


def test_check_fair_price_overpriced():
    """Verify fair price tool flags overpriced offers (>20% above market)."""
    with patch("nodes.tools._resolve_and_freshen") as mock_resolve, \
         patch("nodes.tools.sqlite3.connect") as mock_conn, \
         patch("nodes.tools._fetch_item_row") as mock_row, \
         patch("nodes.tools._fetch_48hr_prices") as mock_48hr, \
         patch("nodes.tools.compute_trend_signal") as mock_trend:

        mock_resolve.return_value = ("Rhino Prime", "set", ["rhino_prime_set"])
        mock_row.return_value = {"item_id": "1", "item_name": "Rhino Prime Set", "url_slug": "rhino_prime_set"}
        mock_48hr.return_value = [{"median_price": 100.0}]
        mock_trend.return_value = {"current_price": 100.0, "mean_price": 95.0}

        # 150p is 50% above 100p market -> Overpriced
        res = check_fair_price.invoke({"item_name": "Rhino Prime", "offered_price": 150.0})
        assert "OVERPRICED" in res
        assert "150p" in res


def test_check_fair_price_fair():
    """Verify fair price tool identifies prices within +-20% as fair."""
    with patch("nodes.tools._resolve_and_freshen") as mock_resolve, \
         patch("nodes.tools.sqlite3.connect") as mock_conn, \
         patch("nodes.tools._fetch_item_row") as mock_row, \
         patch("nodes.tools._fetch_48hr_prices") as mock_48hr, \
         patch("nodes.tools.compute_trend_signal") as mock_trend:

        mock_resolve.return_value = ("Rhino Prime", "set", ["rhino_prime_set"])
        mock_row.return_value = {"item_id": "1", "item_name": "Rhino Prime Set", "url_slug": "rhino_prime_set"}
        mock_48hr.return_value = [{"median_price": 100.0}]
        mock_trend.return_value = {"current_price": 100.0, "mean_price": 95.0}

        # 105p is 5% above 100p market -> Fair
        res = check_fair_price.invoke({"item_name": "Rhino Prime", "offered_price": 105.0})
        assert "FAIR" in res


def test_compare_set_vs_parts_breakdown():
    """Verify set vs parts calculates difference and margin advice."""
    with patch("nodes.tools._resolve_and_freshen") as mock_resolve, \
         patch("nodes.tools.sqlite3.connect") as mock_conn, \
         patch("nodes.tools.compute_trend_signal") as mock_trend:

        mock_resolve.return_value = ("Rhino Prime", None, ["rhino_prime_set"])
        mock_db = MagicMock()
        mock_conn.return_value = mock_db
        cur = MagicMock()
        mock_db.cursor.return_value = cur

        cur.fetchall.return_value = [
            {"item_id": "s1", "url_slug": "rhino_prime_set", "item_name": "Rhino Prime Set", "component_type": "set"},
            {"item_id": "p1", "url_slug": "rhino_prime_blueprint", "item_name": "Rhino Prime Blueprint", "component_type": "blueprint"},
            {"item_id": "p2", "url_slug": "rhino_prime_chassis", "item_name": "Rhino Prime Chassis", "component_type": "chassis"},
        ]

        def trend_side_effect(item_id, conn):
            if item_id == "s1":
                return {"current_price": 80.0}
            if item_id == "p1":
                return {"current_price": 40.0}
            if item_id == "p2":
                return {"current_price": 55.0}
            return {"current_price": 0.0}

        mock_trend.side_effect = trend_side_effect

        res = compare_set_vs_parts.invoke({"item_name": "Rhino Prime"})
        assert "Set vs. Parts Breakdown" in res
        assert "Set Price (current): 80p" in res
        # Parts sum = 40 + 55 = 95p -> 15p more
        assert "95p" in res
        assert "Selling parts individually yields" in res
