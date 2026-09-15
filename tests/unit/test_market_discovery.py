"""
Unit tests for Market Discovery utility (utils/market_discovery.py).

Tests market filtering, keyword search across titles, liquidity prioritization,
and contract expiration detection.
"""

import pytest
from unittest.mock import patch, MagicMock

from utils.market_discovery import (
    discover_active_market,
    fetch_eligible_markets,
    check_market_status,
    is_market_active,
)


def test_discover_exact_match():
    """Verify exact ticker match when specified."""
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXNFL-26SEP14-KC", "status": "open"},
            {"ticker": "KXINX-26SEP14-5800", "status": "open"},
        ]
        result = discover_active_market(target_preference="KXINX-26SEP14-5800")
        assert result == "KXINX-26SEP14-5800"


def test_discover_sports_keyword():
    """Verify sports keyword matching on ticker."""
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXNFL-26SEP14-KC", "status": "open"},
            {"ticker": "KXINX-26SEP14-5800", "status": "open"},
        ]
        result = discover_active_market(target_preference="NFL")
        assert result == "KXNFL-26SEP14-KC"


def test_discover_category_sports():
    """Verify general 'SPORTS' preference matches any active sport."""
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXMLB-26SEP14-NYY", "status": "open"},
            {"ticker": "KXINX-26SEP14-5800", "status": "open"},
        ]
        result = discover_active_market(target_preference="SPORTS")
        assert result == "KXMLB-26SEP14-NYY"


def test_discover_fallback_when_sports_unavailable():
    """Verify fallback to macro index when requested category is absent."""
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXINX-26SEP14-5800", "status": "open"},
        ]
        result = discover_active_market(target_preference="NFL")
        assert result == "KXINX-26SEP14-5800"


def test_discover_exclude_tickers():
    """Verify excluded tickers are skipped."""
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXNFL-FIRST", "status": "open"},
            {"ticker": "KXNFL-SECOND", "status": "open"},
        ]
        result = discover_active_market(
            target_preference="NFL",
            exclude_tickers=["KXNFL-FIRST"]
        )
        assert result == "KXNFL-SECOND"


def test_discover_filters_expired_close_time():
    """Verify that markets whose close_time has passed are excluded even if status is 'open'."""
    mock_markets = [
        {"ticker": "KXPLATINUMH-EXPIRED", "status": "open", "close_time": "2020-01-01T21:00:00Z"},
        {"ticker": "KXNFL-ACTIVE", "status": "open", "close_time": "2030-01-01T21:00:00Z"},
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": mock_markets}
        mock_get.return_value = mock_resp

        eligible = fetch_eligible_markets()
        tickers = [m["ticker"] for m in eligible]
        assert "KXPLATINUMH-EXPIRED" not in tickers
        assert "KXNFL-ACTIVE" in tickers


def test_fetch_eligible_markets_queries_open_status_param():
    """Ensure fetch_eligible_markets explicitly passes status='open' to Kalshi REST API."""
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": []}
        mock_get.return_value = mock_resp

        fetch_eligible_markets(limit=500)

        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        assert kwargs["params"].get("status") == "open"
        assert kwargs["params"].get("limit") == 500


def test_fetch_eligible_markets_filters_non_open_statuses():
    """Verify that fetch_eligible_markets strictly admits 'open' status and discards 'active', 'closed', etc."""
    mock_markets = [
        {"ticker": "MKT-OPEN", "status": "open"},
        {"ticker": "MKT-ACTIVE", "status": "active"},
        {"ticker": "MKT-CLOSED", "status": "closed"},
        {"ticker": "MKT-SETTLED", "status": "settled"},
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": mock_markets}
        mock_get.return_value = mock_resp

        eligible = fetch_eligible_markets()
        assert [m["ticker"] for m in eligible] == ["MKT-OPEN"]


def test_discover_matches_title_and_subtitle():
    """Verify that keywords match against market title even if ticker doesn't contain the keyword."""
    mock_markets = [
        {"ticker": "KXSUPERBOWL-KC", "title": "Will Kansas City win the NFL Super Bowl?", "status": "open"},
        {"ticker": "KXWEATHER-MIA", "title": "Miami Temperature", "status": "open"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets):
        result = discover_active_market(target_preference="NFL")
        assert result == "KXSUPERBOWL-KC"


def test_discover_prioritizes_liquidity():
    """Verify that markets with positive volume/open interest are chosen over zero-volume contracts."""
    mock_markets = [
        {"ticker": "KXNFL-DEAD", "title": "NFL Matchup", "volume": 0, "open_interest": 0, "status": "open"},
        {"ticker": "KXNFL-LIQUID", "title": "NFL Matchup", "volume": 15000, "open_interest": 500, "status": "open"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets):
        result = discover_active_market(target_preference="NFL")
        assert result == "KXNFL-LIQUID"


def test_check_market_status_active():
    """Verify confirmed active status."""
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"market": {"ticker": "TEST-1", "status": "active"}}
        mock_get.return_value = mock_resp

        status = check_market_status("TEST-1")
        assert status == "active"
        assert is_market_active("TEST-1") is True


def test_check_market_status_closed():
    """Verify confirmed settled/closed status."""
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"market": {"ticker": "TEST-1", "status": "settled"}}
        mock_get.return_value = mock_resp

        status = check_market_status("TEST-1")
        assert status == "settled"
        assert is_market_active("TEST-1") is False


def test_check_market_status_expired_close_time():
    """Verify check_market_status marks past close_time contracts as closed even if API returns open."""
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXPLATINUMH-EXPIRED",
                "status": "open",
                "close_time": "2020-01-01T21:00:00Z"
            }
        }
        mock_get.return_value = mock_resp

        status = check_market_status("KXPLATINUMH-EXPIRED")
        assert status == "closed"
        assert is_market_active("KXPLATINUMH-EXPIRED") is False


def test_check_market_status_unknown():
    """Verify unconfirmed market status returns None on network error."""
    with patch("requests.get", side_effect=RuntimeError("Network error")):
        assert check_market_status("TEST-1") is None
        assert is_market_active("TEST-1") is None
