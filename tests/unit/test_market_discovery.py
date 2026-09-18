"""
Unit tests for Market Discovery utility (utils/market_discovery.py).

Tests market filtering, keyword search across titles, liquidity prioritization,
and contract expiration detection.
"""

import datetime
import pytest
import requests
from unittest.mock import patch, MagicMock

from utils.market_discovery import (
    discover_active_market,
    fetch_eligible_markets,
    check_market_status,
    is_market_active,
    check_orderbook_has_quotes,
    _liquidity_key,
    _select_best_market,
    SportsSeasonRouter,
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


def test_discover_idles_when_sports_unavailable():
    """Verify market discovery idles (returns None) rather than falling back to macro indices when sports are absent."""
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXINX-26SEP14-5800", "status": "open"},
        ]
        result = discover_active_market(target_preference="NFL")
        assert result is None


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


def test_liquidity_key_v2_fields_and_horizon_weighting():
    """Verify _liquidity_key parses v2 float string fields and applies horizon multiplier."""
    # Near term (3 days) contract with v2 float string fields
    near_term_market = {
        "ticker": "KXNFLGAME-26SEP17DETBUF",
        "volume_fp": "25000.50",
        "open_interest_fp": "1200.00",
        "yes_bid_dollars": "0.3200",
        "yes_ask_dollars": "0.3500",
        "close_time": "2026-09-18T20:00:00Z",  # near-term
    }

    # Distant futures prop (e.g. 2 years out) with high legacy volume
    distant_market = {
        "ticker": "KXNFLENDSTREAK-40NYJ-2627",
        "volume": 50000,
        "open_interest": 2000,
        "yes_bid": 10,
        "yes_ask": 25,
        "close_time": "2029-01-01T00:00:00Z",  # distant horizon (>365d)
    }

    score_near = _liquidity_key(near_term_market)
    score_distant = _liquidity_key(distant_market)

    # Near term weekly game line with two-sided quotes & series bonus should heavily outscore distant prop
    assert score_near > score_distant


def test_check_orderbook_has_quotes_fp_and_legacy():
    """Verify check_orderbook_has_quotes handles both orderbook_fp and legacy orderbook responses."""
    # 1. Successful v2 orderbook_fp response (yes_dollars or yes_dollars_fp)
    mock_fp_resp = MagicMock()
    mock_fp_resp.status_code = 200
    mock_fp_resp.json.return_value = {
        "orderbook_fp": {
            "yes_dollars_fp": [["0.3200", "150.00"]],
            "no_dollars_fp": [["0.6500", "80.00"]],
        }
    }
    with patch("requests.get", return_value=mock_fp_resp):
        assert check_orderbook_has_quotes("KXNFLGAME-26SEP17DETBUF") is True

    # 2. Empty/one-sided orderbook_fp response
    mock_empty_resp = MagicMock()
    mock_empty_resp.status_code = 200
    mock_empty_resp.json.return_value = {
        "orderbook_fp": {
            "yes_dollars": [],
            "no_dollars": [],
        }
    }
    with patch("requests.get", return_value=mock_empty_resp):
        assert check_orderbook_has_quotes("KXNFLENDSTREAK-40NYJ-2627") is False

    # 3. Network error returns False safely
    with patch("requests.get", side_effect=requests.RequestException("Timeout")):
        assert check_orderbook_has_quotes("KXNFLGAME-ERROR") is False


def test_select_best_market_preflight_filters_empty_orderbook():
    """Verify _select_best_market selects the first candidate that actually has resting quotes."""
    candidates = [
        {"ticker": "KXNFL-STARVED", "volume_fp": "99999.00", "yes_bid_dollars": "0.1000", "yes_ask_dollars": "0.2000"},
        {"ticker": "KXNFL-ACTIVE", "volume_fp": "1000.00", "yes_bid_dollars": "0.4500", "yes_ask_dollars": "0.5000"},
    ]

    def mock_check(ticker):
        return ticker == "KXNFL-ACTIVE"

    with patch("utils.market_discovery.check_orderbook_has_quotes", side_effect=mock_check):
        selected = _select_best_market(candidates, preflight_check=True)
        assert selected == "KXNFL-ACTIVE"


def test_discover_active_market_prioritizes_kxnflgame_series():
    """Verify discover_active_market prioritizes KXNFLGAME series when NFL or sports is targeted."""
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch, \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        
        mock_fetch.return_value = [
            {"ticker": "KXNFLPASSYDS-MAHOMES-300", "series_ticker": "KXNFLPASSYDS", "status": "open", "volume_fp": "90000.00"},
            {"ticker": "KXNFLGAME-26SEP17DETBUF", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "50000.00"},
        ]
        result = discover_active_market(target_preference="NFL")
        assert result == "KXNFLGAME-26SEP17DETBUF"
        mock_fetch.assert_called_once()


def test_sports_season_router_calendar_priorities():
    """Verify SportsSeasonRouter resolves seasonal league priorities correctly by month."""
    import datetime

    # Early Fall: September (month 9) -> NFL, MLB (NBA not started)
    dt_sep = datetime.datetime(2026, 9, 15, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_sep) == ["NFL", "MLB"]

    # Fall/Winter: October (month 10) -> NFL, NBA, MLB
    dt_oct = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_oct) == ["NFL", "NBA", "MLB"]

    # Mid-Winter: January (month 1) -> NFL, NBA
    dt_jan = datetime.datetime(2026, 1, 10, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_jan) == ["NFL", "NBA"]

    # Spring: April (month 4) -> NBA, MLB
    dt_apr = datetime.datetime(2026, 4, 15, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_apr) == ["NBA", "MLB"]

    # Summer Lull: July (month 7) -> MLB
    dt_jul = datetime.datetime(2026, 7, 15, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_jul) == ["MLB"]

    # Late Summer: August (month 8) -> MLB only (NFL preseason excluded)
    dt_aug = datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_aug) == ["MLB"]


def test_sports_season_router_full_product_suites():
    """Verify in-season series include Game Lines and Player Props for all major leagues during active overlap."""
    import datetime
    dt_oct = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc)
    series = SportsSeasonRouter.get_in_season_series(dt_oct)
    # NFL Game Lines & Player Props
    assert "KXNFLGAME" in series
    assert "KXNFLSPREAD" in series
    assert "KXNFLTOTAL" in series
    assert "KXNFLTD" in series
    assert "KXNFLPASSYDS" in series
    assert "KXNFLRSHYDS" in series
    assert "KXNFLRECYDS" in series
    assert "KXNFLPASSTDS" in series
    # NBA Lines & Props
    assert "KXNBAGAME" in series
    assert "KXNBASPREAD" in series
    assert "KXNBATOTAL" in series
    assert "KXNBAPTS" in series
    assert "KXNBAREB" in series
    assert "KXNBAAST" in series
    assert "KXNBA3PT" in series
    assert "KXNBAPRA" in series
    # MLB Lines & Props
    assert "KXMLBGAME" in series
    assert "KXMLBSPREAD" in series
    assert "KXMLBTOTAL" in series
    assert "KXMLBKS" in series
    assert "KXMLBHR" in series
    assert "KXMLBHIT" in series
    assert "KXMLBTB" in series

    # Test league-specific series getter
    nfl_suite = SportsSeasonRouter.get_series_for_league("NFL")
    assert "KXNFLGAME" in nfl_suite
    assert "KXNFLTD" in nfl_suite
    assert "KXNFLRSHYDS" in nfl_suite
    assert "KXNFLPASSTDS" in nfl_suite

    mlb_suite = SportsSeasonRouter.get_series_for_league("MLB")
    assert "KXMLBGAME" in mlb_suite
    assert "KXMLBSPREAD" in mlb_suite
    assert "KXMLBKS" in mlb_suite
    assert "KXMLBHIT" in mlb_suite
    assert "KXMLBTB" in mlb_suite


def test_select_best_market_returns_none_when_preflight_fails_all_candidates():
    """Verify _select_best_market returns None (not pool[0]) when all candidates fail pre-flight orderbook check."""
    candidates = [
        {"ticker": "KXNFL-STARVED-1", "volume_fp": "10000.00"},
        {"ticker": "KXNFL-STARVED-2", "volume_fp": "5000.00"},
    ]
    with patch("utils.market_discovery.check_orderbook_has_quotes", return_value=False):
        # Preflight check enabled -> should return None rather than unquoted pool[0]
        assert _select_best_market(candidates, preflight_check=True) is None
        # Preflight check disabled -> falls back to top liquidity candidate
        assert _select_best_market(candidates, preflight_check=False) == "KXNFL-STARVED-1"


def test_discover_active_market_cascades_to_player_props():
    """Verify waterfall cascades from game lines to player props within the same league when game lines are unquoted."""
    mock_markets = [
        {"ticker": "KXNFLPASSYDS-MAHOMES-300", "series_ticker": "KXNFLPASSYDS", "status": "open", "volume_fp": "2000.00"}
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        result = discover_active_market(target_preference="NFL")
        assert result == "KXNFLPASSYDS-MAHOMES-300"


def test_secondary_league_reachable_when_primary_league_game_lines_unquoted():
    """Verify NBA Game Lines are reached and selected when NFL Game Lines are dormant in October."""
    import datetime
    mock_markets = [
        {"ticker": "KXNFLGAME-DORMANT-1", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "5000.00"},
        {"ticker": "KXNBAGAME-ACTIVE-1", "series_ticker": "KXNBAGAME", "status": "open", "volume_fp": "4000.00"},
    ]
    def mock_quotes(ticker):
        return ticker == "KXNBAGAME-ACTIVE-1"

    dt_oct = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc)
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", side_effect=mock_quotes), \
         patch("utils.market_discovery.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = dt_oct
        mock_dt.datetime.timezone = datetime.timezone
        result = discover_active_market(target_preference="SPORTS")
        assert result == "KXNBAGAME-ACTIVE-1"


def test_fetch_eligible_markets_excludes_nhl_tickers():
    """Verify that markets with KXNHL tickers are filtered out."""
    mock_markets = [
        {"ticker": "KXNHL-26SEP14-TORBOS", "status": "open", "close_time": "2030-01-01T00:00:00Z"},
        {"ticker": "KXNFL-26SEP14-KC", "status": "open", "close_time": "2030-01-01T00:00:00Z"},
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": mock_markets}
        mock_get.return_value = mock_resp

        eligible = fetch_eligible_markets()
        tickers = [m["ticker"] for m in eligible]
        assert "KXNHL-26SEP14-TORBOS" not in tickers
        assert "KXNFL-26SEP14-KC" in tickers


def test_discovery_wide_probe_budget_caps_total_requests():
    """Verify that an all-dormant cascade strictly honors max_total_probes across all series."""
    mock_markets = [
        {"ticker": f"KXNFLGAME-CAND-{i}", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "1000.00"} for i in range(5)
    ] + [
        {"ticker": f"KXNFLSPREAD-CAND-{i}", "series_ticker": "KXNFLSPREAD", "status": "open", "volume_fp": "1000.00"} for i in range(5)
    ] + [
        {"ticker": f"KXNBAGAME-CAND-{i}", "series_ticker": "KXNBAGAME", "status": "open", "volume_fp": "1000.00"} for i in range(5)
    ] + [
        {"ticker": f"KXMLBGAME-CAND-{i}", "series_ticker": "KXMLBGAME", "status": "open", "volume_fp": "1000.00"} for i in range(5)
    ]

    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=False) as mock_probe:
        # Request with max_total_probes=6 across all in-season series
        result = discover_active_market(target_preference="SPORTS", max_total_probes=6)
        assert result is None
        # Assert the total probes across all series never exceeded the configured quota of 6
        assert mock_probe.call_count == 6


def test_per_series_probe_limit_caps_probes_per_series():
    """Verify that a single series with many candidates only probes up to DEFAULT_MAX_PROBES_PER_SERIES."""
    mock_markets = [
        {"ticker": f"KXNFLGAME-CAND-{i}", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": f"{10000 - i * 100}.00"}
        for i in range(10)
    ]

    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=False) as mock_probe:
        result = discover_active_market(target_preference="NFL", max_total_probes=10)
        assert result is None
        # KXNFLGAME had 10 candidates, but per-series limit is 2; subsequent series had 0
        assert mock_probe.call_count == 2


def test_discover_exact_match_with_preflight_check():
    """Verify exact match screens orderbook quotes when preflight_check is True."""
    mock_markets = [
        {"ticker": "KXNFL-TARGET", "status": "open"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets):
        # Case 1: Target ticker has quotes -> selected
        with patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
            res = discover_active_market(target_preference="KXNFL-TARGET", preflight_check=True)
            assert res == "KXNFL-TARGET"

        # Case 2: Target ticker has no quotes -> not returned (idles)
        with patch("utils.market_discovery.check_orderbook_has_quotes", return_value=False):
            res = discover_active_market(target_preference="KXNFL-TARGET", preflight_check=True)
            assert res is None

        # Case 3: Target ticker has quotes but probe budget is 0 -> does not probe, returns None
        with patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True) as mock_quotes:
            res = discover_active_market(target_preference="KXNFL-TARGET", preflight_check=True, max_total_probes=0)
            assert res is None
            mock_quotes.assert_not_called()


def test_tier_1a_probes_all_league_primary_moneylines_before_secondary_lines():
    """
    Verify that in a multi-league overlap (e.g. October with NFL, NBA, MLB),
    Tier 1A checks primary moneylines across all leagues before secondary lines
    so dormant NFL does not starve MLB World Series or NBA.
    """
    mock_markets = [
        {"ticker": "KXNFLGAME-OCT-1", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "5000.00"},
        {"ticker": "KXNFLSPREAD-OCT-1", "series_ticker": "KXNFLSPREAD", "status": "open", "volume_fp": "8000.00"},
        {"ticker": "KXNBAGAME-OCT-1", "series_ticker": "KXNBAGAME", "status": "open", "volume_fp": "4000.00"},
        {"ticker": "KXMLBGAME-WS-1", "series_ticker": "KXMLBGAME", "status": "open", "volume_fp": "9000.00"},
    ]

    # NFL moneyline and NBA moneyline are dormant; MLB World Series has live quotes
    def mock_quotes(ticker: str) -> bool:
        if "KXMLBGAME" in ticker:
            return True
        return False

    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL", "NBA", "MLB"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes", side_effect=mock_quotes) as mock_probe:
        selected = discover_active_market(target_preference="SPORTS", preflight_check=True)
        assert selected == "KXMLBGAME-WS-1"
        # Verify probing order: KXNFLGAME was probed, KXNBAGAME was probed, KXMLBGAME was probed
        probed_tickers = [call.args[0] for call in mock_probe.call_args_list]
        assert "KXNFLGAME-OCT-1" in probed_tickers
        assert "KXNBAGAME-OCT-1" in probed_tickers
        assert "KXMLBGAME-WS-1" in probed_tickers
        # KXNFLSPREAD was NOT probed because KXMLBGAME was found in Tier 1A
        assert "KXNFLSPREAD-OCT-1" not in probed_tickers


def test_excluded_exact_sports_ticker_rotates_to_league_suite():
    """
    Verify that when an exact sports ticker is excluded during auto-rotation (e.g. after settlement
    or starvation), discovery routes to its league suite (e.g. NFL) to find an active replacement.
    """
    mock_markets = [
        {"ticker": "KXNFLGAME-OLD-SETTLED", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "50000.00"},
        {"ticker": "KXNFLGAME-NEW-ACTIVE", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "40000.00"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        # target_preference retains the original exact ticker, but that ticker is now excluded
        selected = discover_active_market(
            target_preference="KXNFLGAME-OLD-SETTLED",
            exclude_tickers=["KXNFLGAME-OLD-SETTLED"],
            preflight_check=True,
        )
        assert selected == "KXNFLGAME-NEW-ACTIVE"


def test_excluded_exact_non_sports_ticker_rotates_to_seasonal_fallback():
    """
    Verify that when an exact non-sports ticker is excluded and has no keyword matches,
    discovery routes to the seasonal sports fallback before idling so auto-rotation can find a replacement.
    """
    mock_markets = [
        {"ticker": "FED-RATE-OLD-SETTLED", "status": "open", "volume_fp": "50000.00"},
        {"ticker": "KXNFLGAME-ACTIVE-1", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "20000.00"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        selected = discover_active_market(
            target_preference="FED-RATE-OLD-SETTLED",
            exclude_tickers=["FED-RATE-OLD-SETTLED"],
            preflight_check=True,
        )
        assert selected == "KXNFLGAME-ACTIVE-1"


def test_discover_filters_markets_exceeding_weekly_horizon():
    """
    Verify that automated sports discovery strictly filters out markets with close_time > 8 days
    (e.g. season-ending props like KXNFLENDSTREAK) and selects near-term weekly game lines.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    near_term_close = (now + datetime.timedelta(days=3)).isoformat()
    distant_close = (now + datetime.timedelta(days=150)).isoformat()

    mock_markets = [
        {
            "ticker": "KXNFLENDSTREAK-40NYJ-2627",
            "series_ticker": "KXNFLENDSTREAK",
            "status": "open",
            "close_time": distant_close,
            "volume_fp": "100000.00",
            "yes_bid_dollars": "0.19",
            "yes_ask_dollars": "0.23",
        },
        {
            "ticker": "KXNFLGAME-26SEP20-DETBUF",
            "series_ticker": "KXNFLGAME",
            "status": "open",
            "close_time": near_term_close,
            "volume_fp": "50000.00",
            "yes_bid_dollars": "0.48",
            "yes_ask_dollars": "0.52",
        },
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]):
        selected = discover_active_market(target_preference="NFL", preflight_check=True)
        assert selected == "KXNFLGAME-26SEP20-DETBUF"


def test_discover_respects_custom_max_expiration_days():
    """
    Verify that passing max_expiration_days filters out markets beyond the custom threshold.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    one_day_close = (now + datetime.timedelta(days=1)).isoformat()
    four_day_close = (now + datetime.timedelta(days=4)).isoformat()

    mock_markets = [
        {
            "ticker": "KXMLBGAME-TODAY",
            "series_ticker": "KXMLBGAME",
            "status": "open",
            "close_time": one_day_close,
            "volume_fp": "10000.00",
        },
        {
            "ticker": "KXMLBGAME-FOURDAYS",
            "series_ticker": "KXMLBGAME",
            "status": "open",
            "close_time": four_day_close,
            "volume_fp": "20000.00",
        },
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["MLB"]):
        selected = discover_active_market(
            target_preference="MLB",
            preflight_check=True,
            max_expiration_days=2.0,
        )
        assert selected == "KXMLBGAME-TODAY"


def test_discover_exact_match_bypasses_horizon_filter():
    """
    Verify that an explicitly targeted exact contract is selected even if its close_time
    exceeds the weekly horizon (e.g. operator explicitly targeting a long-term future).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    distant_close = (now + datetime.timedelta(days=150)).isoformat()

    mock_markets = [
        {
            "ticker": "KXNFLENDSTREAK-40NYJ-2627",
            "series_ticker": "KXNFLENDSTREAK",
            "status": "open",
            "close_time": distant_close,
            "volume_fp": "100000.00",
        },
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        selected = discover_active_market(
            target_preference="KXNFLENDSTREAK-40NYJ-2627",
            preflight_check=True,
            max_expiration_days=8.0,
        )
        assert selected == "KXNFLENDSTREAK-40NYJ-2627"


def test_tier_3_catchall_filters_multi_month_futures():
    """
    Verify that Tier 3 general league catch-all strictly drops markets beyond the weekly horizon
    and idles (returns None) if no near-term markets exist, rather than selecting distant futures.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    distant_close = (now + datetime.timedelta(days=150)).isoformat()

    mock_markets = [
        {
            "ticker": "KXNFLENDSTREAK-40NYJ-2627",
            "series_ticker": "KXNFLENDSTREAK",
            "status": "open",
            "close_time": distant_close,
            "volume_fp": "100000.00",
        },
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]):
        selected = discover_active_market(target_preference="NFL", preflight_check=True)
        assert selected is None


def test_targeted_series_fetch_fallback_when_events_misses_series():
    """
    Verify that if the initial global markets list lacks the active series,
    discovery triggers a targeted series query to retrieve the weekly game lines.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    near_term_close = (now + datetime.timedelta(days=2)).isoformat()

    global_markets = [
        {"ticker": "POLITICS-2028-ELECTION", "status": "open"},
    ]
    targeted_nfl_games = [
        {
            "ticker": "KXNFLGAME-26SEP20-NEBUF",
            "series_ticker": "KXNFLGAME",
            "status": "open",
            "close_time": near_term_close,
            "volume_fp": "35000.00",
        },
    ]

    def mock_fetch(limit=1000, series_ticker=None, max_expiration_days=None):
        if series_ticker == "KXNFLGAME":
            return targeted_nfl_games
        return global_markets

    with patch("utils.market_discovery.fetch_eligible_markets", side_effect=mock_fetch), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]):
        selected = discover_active_market(target_preference="NFL", preflight_check=True)
        assert selected == "KXNFLGAME-26SEP20-NEBUF"


def test_fetch_eligible_markets_with_max_expiration_days():
    """
    Verify fetch_eligible_markets filters out markets beyond max_expiration_days directly at fetch time.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_markets = [
        {"ticker": "KXNFL-NEAR", "status": "open", "close_time": (now + datetime.timedelta(days=3)).isoformat()},
        {"ticker": "KXNFL-DISTANT", "status": "open", "close_time": (now + datetime.timedelta(days=30)).isoformat()},
    ]
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": mock_markets}
        mock_get.return_value = mock_resp

        eligible = fetch_eligible_markets(max_expiration_days=8.0)
        tickers = [m["ticker"] for m in eligible]
        assert "KXNFL-NEAR" in tickers
        assert "KXNFL-DISTANT" not in tickers





