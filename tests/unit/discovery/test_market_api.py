"""
Unit Tests for Market API Client

Tests Kalshi vendor API interaction:
- Status query parameter propagation ('open')
- Multi-status acceptance ('open' and 'active')
- Events endpoint pagination and fallback to /markets
- Limit bounding and nested market mapping
- Status checks and active/settled detection
- Legacy patch seams and standalone fallback mechanisms
"""

import certifi
import pytest
import requests
import sys
from unittest.mock import patch, MagicMock

import config
from utils import market_api as ma
from utils import market_discovery as md
from utils.market_discovery import (
    fetch_eligible_markets,
    check_market_status,
    is_market_active,
)


def test_fetch_eligible_markets_queries_open_status_param():
    """Ensure fetch_eligible_markets explicitly passes status='open' to Kalshi REST API endpoints."""
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": []}
        mock_get.return_value = mock_resp

        fetch_eligible_markets(limit=500)

        # When /events returns no events, both /events and fallback /markets are queried
        assert mock_get.call_count == 2
        events_call, markets_call = mock_get.call_args_list

        # Call 1: primary query to /events
        assert "events" in events_call[0][0]
        assert events_call[1]["params"].get("status") == "open"
        assert events_call[1]["params"].get("with_nested_markets") == "true"

        # Call 2: fallback query to /markets
        assert "markets" in markets_call[0][0]
        assert markets_call[1]["params"].get("status") == "open"
        assert markets_call[1]["params"].get("limit") == 500


def test_fetch_eligible_markets_primary_events_bypasses_markets_call():
    """Verify that when /events succeeds and yields markets, fallback /markets is not invoked and title is inherited."""
    mock_events = [
        {
            "event_ticker": "KXNFL-SUPERBOWL",
            "title": "Super Bowl Champion",
            "subtitle": "NFL 2026",
            "markets": [
                {"ticker": "KXNFL-KC", "status": "active", "close_time": "2030-01-01T00:00:00Z"},
                {"ticker": "KXMVE-SHARD", "status": "active", "close_time": "2030-01-01T00:00:00Z"},
            ],
        }
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"events": mock_events}
        mock_get.return_value = mock_resp

        eligible = fetch_eligible_markets()
        mock_get.assert_called_once()
        assert "events" in mock_get.call_args[0][0]
        assert len(eligible) == 1
        assert eligible[0]["ticker"] == "KXNFL-KC"
        assert eligible[0]["title"] == "Super Bowl Champion"
        assert eligible[0]["subtitle"] == "NFL 2026"


def test_fetch_eligible_markets_events_timeout_falls_back_to_markets():
    """Verify that when /events times out or raises an exception, the bot falls back to /markets and returns eligible markets."""
    mock_markets = [
        {"ticker": "FALLBACK-MKT-1", "status": "active", "close_time": "2030-01-01T00:00:00Z"},
    ]
    markets_resp = MagicMock()
    markets_resp.status_code = 200
    markets_resp.json.return_value = {"markets": mock_markets}

    with patch("requests.get") as mock_get:
        mock_get.side_effect = [
            requests.exceptions.Timeout("Connection timed out"),
            markets_resp,
        ]

        eligible = fetch_eligible_markets(limit=500)

        assert mock_get.call_count == 2
        events_call, markets_call = mock_get.call_args_list

        assert "events" in events_call[0][0]
        assert "markets" in markets_call[0][0]
        assert markets_call[1]["params"].get("status") == "open"
        assert markets_call[1]["params"].get("limit") == 500

        assert len(eligible) == 1
        assert eligible[0]["ticker"] == "FALLBACK-MKT-1"


def test_fetch_eligible_markets_events_pagination():
    """Verify that fetch_eligible_markets follows cursor on /events across pages until limit or cursor exhaustion."""
    page1_resp = MagicMock()
    page1_resp.status_code = 200
    page1_resp.json.return_value = {
        "cursor": "cursor_page_2",
        "events": [
            {
                "event_ticker": "EVENT-PAGE-1",
                "title": "Event Page 1",
                "markets": [
                    {"ticker": "MKT-P1", "status": "active", "close_time": "2030-01-01T00:00:00Z"}
                ],
            }
        ],
    }

    page2_resp = MagicMock()
    page2_resp.status_code = 200
    page2_resp.json.return_value = {
        "cursor": None,
        "events": [
            {
                "event_ticker": "EVENT-PAGE-2",
                "title": "Event Page 2",
                "markets": [
                    {"ticker": "MKT-P2", "status": "active", "close_time": "2030-01-01T00:00:00Z"}
                ],
            }
        ],
    }

    with patch("requests.get") as mock_get:
        mock_get.side_effect = [page1_resp, page2_resp]

        eligible = fetch_eligible_markets(limit=10)

        assert mock_get.call_count == 2
        call1, call2 = mock_get.call_args_list

        assert "cursor" not in call1[1]["params"]
        assert call2[1]["params"].get("cursor") == "cursor_page_2"

        tickers = [m["ticker"] for m in eligible]
        assert tickers == ["MKT-P1", "MKT-P2"]


def test_fetch_eligible_markets_honors_limit_bound_on_events():
    """Verify that fetch_eligible_markets bounds collected nested markets to limit and stops iteration."""
    mock_events = [
        {
            "event_ticker": "EVENT-MULTI",
            "title": "Multi Market Event",
            "markets": [
                {"ticker": f"MKT-{i}", "status": "active", "close_time": "2030-01-01T00:00:00Z"}
                for i in range(10)
            ],
        }
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"events": mock_events, "cursor": "next_page_cursor"}
        mock_get.return_value = mock_resp

        eligible = fetch_eligible_markets(limit=3)

        # Should only call once because limit (3) is satisfied by the first event
        mock_get.assert_called_once()
        assert len(eligible) == 3
        assert [m["ticker"] for m in eligible] == ["MKT-0", "MKT-1", "MKT-2"]


def test_fetch_eligible_markets_accepts_both_open_and_active():
    """
    Verify that fetch_eligible_markets admits both 'open' and 'active' statuses
    (required due to Kalshi API response payload quirk) while strictly discarding 'closed' and 'settled'.
    """
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
        assert [m["ticker"] for m in eligible] == ["MKT-OPEN", "MKT-ACTIVE"]


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


def test_market_api_missing_coverage_branches():
    """Verify market API series filtering, non-200 responses, observed statuses warning, and errors."""
    from utils.market_api import fetch_eligible_markets as api_fetch_eligible_markets, check_market_status as api_check_market_status

    # 1. /events query with series_ticker
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "events": [{"markets": [{"ticker": "KXNFLGAME-1", "status": "open"}]}]
        }
        mock_get.return_value = mock_resp
        res = api_fetch_eligible_markets(series_ticker="KXNFLGAME")
        assert len(res) == 1
        assert res[0]["ticker"] == "KXNFLGAME-1"

    # 2. Non-200 on /events falling back to /markets with series_ticker
    with patch("requests.get") as mock_get:
        resp_err = MagicMock()
        resp_err.status_code = 500
        resp_err.text = "Internal error"
        resp_fallback = MagicMock()
        resp_fallback.status_code = 200
        resp_fallback.json.return_value = {"markets": [{"ticker": "KXNFLGAME-FALLBACK", "status": "open"}]}
        mock_get.side_effect = [resp_err, resp_fallback]
        res = api_fetch_eligible_markets(series_ticker="KXNFLGAME")
        assert len(res) == 1
        assert res[0]["ticker"] == "KXNFLGAME-FALLBACK"

    # 3. All returned markets filtered out triggers observed statuses warning
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": [{"ticker": "KX-CLOSED", "status": "closed"}]}
        mock_get.return_value = mock_resp
        res = api_fetch_eligible_markets()
        assert res == []

    # 4. Top-level exception in fetch_eligible_markets returns []
    with patch("requests.get", side_effect=RuntimeError("Fatal error")):
        assert api_fetch_eligible_markets() == []

    # 5. Non-200 in check_market_status returns None
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp
        assert api_check_market_status("INVALID") is None


@pytest.mark.asyncio
async def test_is_market_active_honors_check_market_status_override():
    """Verify is_market_active and is_market_active_async consult check_market_status on market_discovery."""
    from utils.market_discovery import is_market_active, is_market_active_async

    with patch("utils.market_discovery.check_market_status", return_value="closed") as mock_check:
        # Direct sync call from utils.market_discovery
        assert is_market_active("MOCK-TEST") is False
        # Sync call through utils.market_api
        assert ma.is_market_active("MOCK-TEST") is False
        # Async call from utils.market_discovery
        assert await is_market_active_async("MOCK-TEST") is False
        assert mock_check.call_count == 3

    # Test status is None branch
    with patch("utils.market_discovery.check_market_status", return_value=None):
        assert is_market_active("MOCK-TEST") is None
        assert ma.is_market_active("MOCK-TEST") is None

    # Test standalone fallback when utils.market_discovery is temporarily not in sys.modules
    saved_md = sys.modules.pop("utils.market_discovery", None)
    try:
        checker = ma._get_market_status_checker()
        assert callable(checker)
    finally:
        if saved_md is not None:
            sys.modules["utils.market_discovery"] = saved_md


def test_legacy_patch_import_attributes_compatibility():
    """Verify legacy patch/import seams for BASE_URL, requests, and certifi are preserved."""
    # 1. Imports from utils.market_discovery
    from utils.market_discovery import BASE_URL as MD_BASE_URL, requests as md_requests, certifi as md_certifi
    assert MD_BASE_URL == config.BASE_URL
    assert md_requests is requests
    assert md_certifi is certifi
    assert "BASE_URL" in md.__all__
    assert "requests" in md.__all__
    assert "certifi" in md.__all__

    # 2. Patching utils.market_discovery.BASE_URL propagates to API calls
    with patch("utils.market_discovery.BASE_URL", "https://mock.kalshi.trade"), \
         patch("utils.market_discovery.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"market": {"status": "active"}}
        mock_get.return_value = mock_resp

        md.check_market_status("MOCK-TICKER")
        mock_get.assert_called_once()
        called_url = mock_get.call_args[0][0]
        assert called_url.startswith("https://mock.kalshi.trade/trade-api/v2/markets/MOCK-TICKER")

    # 3. Patching utils.market_discovery.certifi.where propagates to API calls
    with patch("utils.market_discovery.certifi.where", return_value="/mock/custom/ca.pem"), \
         patch("utils.market_discovery.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"orderbook": {"yes": [1], "no": [1]}}
        mock_get.return_value = mock_resp

        md.check_orderbook_has_quotes("MOCK-TICKER")
        mock_get.assert_called_once()
        assert mock_get.call_args[1].get("verify") == "/mock/custom/ca.pem"

    # 4. Patching utils.market_discovery.requests.get intercepts fetch_eligible_markets
    with patch("utils.market_discovery.requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"events": []}
        mock_get.return_value = mock_resp

        md.fetch_eligible_markets(limit=5)
        assert mock_get.call_count >= 1

    # 5. Standalone fallbacks when utils.market_discovery is not in sys.modules
    saved_md = sys.modules.pop("utils.market_discovery", None)
    try:
        assert ma._get_base_url() == config.BASE_URL
        assert ma._get_requests() is requests
        assert ma._get_certifi() is certifi
    finally:
        if saved_md is not None:
            sys.modules["utils.market_discovery"] = saved_md
