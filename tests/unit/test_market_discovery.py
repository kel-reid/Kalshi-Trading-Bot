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
        selected = discover_active_market(
            target_preference="NFL",
            preflight_check=True,
            max_expiration_days=8.0,
        )
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
        selected = discover_active_market(
            target_preference="NFL",
            preflight_check=True,
            max_expiration_days=8.0,
        )
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
        selected = discover_active_market(
            target_preference="NFL",
            preflight_check=True,
            max_expiration_days=8.0,
        )
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


def test_parse_iso_timestamp():
    """Verify ISO timestamp parser safely handles aware, naive, Z, and malformed inputs."""
    from utils.market_discovery import _parse_iso_timestamp

    # Aware with Z
    dt_z = _parse_iso_timestamp("2026-09-20T18:00:00Z")
    assert dt_z is not None
    assert dt_z.tzinfo == datetime.timezone.utc
    assert dt_z.year == 2026 and dt_z.month == 9 and dt_z.day == 20

    # Aware with offset: normalized to UTC
    dt_offset = _parse_iso_timestamp("2026-09-20T14:00:00-04:00")
    assert dt_offset is not None
    assert dt_offset.tzinfo == datetime.timezone.utc
    assert dt_offset.hour == 18

    # Offset-naive ISO timestamp should be normalized to UTC
    dt_naive = _parse_iso_timestamp("2026-09-20T18:00:00")
    assert dt_naive is not None
    assert dt_naive.tzinfo == datetime.timezone.utc
    assert dt_naive.hour == 18

    # Edge cases: None, empty string, whitespace, non-date string
    assert _parse_iso_timestamp(None) is None
    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("   ") is None
    assert _parse_iso_timestamp("invalid-date-format") is None
    assert _parse_iso_timestamp(123456789) is None


def test_is_within_horizon_fails_closed_on_corrupt_or_malformed_timestamps():
    """Verify _is_within_horizon fails closed (returns False) on invalid/unparseable timestamps."""
    from utils.market_discovery import _is_within_horizon

    # Corrupt / malformed close_time must return False
    assert _is_within_horizon({"close_time": "invalid-timestamp"}, max_days=8.0) is False
    assert _is_within_horizon({"expiration_time": "garbage_date_format"}, max_days=8.0) is False

    # Missing close_time maintains backward compatibility for minimal test fixtures
    assert _is_within_horizon({}, max_days=8.0) is True
    assert _is_within_horizon({"close_time": ""}, max_days=8.0) is True
    assert _is_within_horizon({"close_time": "   "}, max_days=8.0) is True

    # When max_days is None, everything is within horizon
    assert _is_within_horizon({"close_time": "invalid-timestamp"}, max_days=None) is True


def test_is_within_horizon_boundary_and_naive_timestamp_handling():
    """Verify _is_within_horizon accurately handles naive timestamps and strict boundary checks."""
    from utils.market_discovery import _is_within_horizon

    now_utc = datetime.datetime(2026, 9, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # Naive timestamp 2 days in the future (within 8 day horizon)
    naive_future = {"close_time": "2026-09-20T12:00:00"}
    assert _is_within_horizon(naive_future, max_days=8.0, now_utc=now_utc) is True

    # Naive timestamp 2 days in the past (expired, should return False)
    naive_past = {"close_time": "2026-09-16T12:00:00"}
    assert _is_within_horizon(naive_past, max_days=8.0, now_utc=now_utc) is False

    # Naive timestamp 10 days in the future (beyond 8 day horizon, should return False)
    naive_distant = {"close_time": "2026-09-28T12:00:00"}
    assert _is_within_horizon(naive_distant, max_days=8.0, now_utc=now_utc) is False

    # Exact boundary: exactly at now_utc (0 seconds remaining) -> True
    boundary_exact_now = {"close_time": "2026-09-18T12:00:00Z"}
    assert _is_within_horizon(boundary_exact_now, max_days=8.0, now_utc=now_utc) is True

    # Exact boundary: exactly at now_utc + 8 days -> True
    boundary_exact_max = {"close_time": "2026-09-26T12:00:00Z"}
    assert _is_within_horizon(boundary_exact_max, max_days=8.0, now_utc=now_utc) is True

    # Beyond boundary: now_utc + 8 days + 1 second -> False
    boundary_beyond = {"close_time": "2026-09-26T12:00:01Z"}
    assert _is_within_horizon(boundary_beyond, max_days=8.0, now_utc=now_utc) is False


def test_targeted_series_fallback_deduplication_and_budget_bounding():
    """
    Verify _ensure_series_markets deduplicates repeated requests for the same series
    and stays strictly bounded by max_targeted_series_fallbacks within the shared discovery probe budget.
    """
    mock_calls = []

    def mock_fetch(limit=1000, series_ticker=None, max_expiration_days=None):
        if series_ticker:
            mock_calls.append(series_ticker)
            return []
        return [{"ticker": "KXNFL-26SEP14-KC", "status": "open"}]

    with patch("utils.market_discovery.fetch_eligible_markets", side_effect=mock_fetch), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]):

        # Run discovery with budget of at most 2 targeted series fallback queries
        selected = discover_active_market(
            target_preference="NFL",
            preflight_check=True,
            max_targeted_series_fallbacks=2,
            max_total_probes=10,
        )

        # Ensure that no series was queried more than once
        assert len(mock_calls) == len(set(mock_calls))
        # Ensure total fallback calls did not exceed the fallback ceiling
        assert len(mock_calls) <= 2
        # Ensure Tier 3 was reached and selected despite empty targeted series responses
        assert selected == "KXNFL-26SEP14-KC"


def test_config_get_float_env_defensive_parsing():
    """Verify _get_float_env in config safely falls back on corrupt, non-finite, or non-positive strings."""
    import os
    from config import _get_float_env

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "invalid_number"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": ""}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "   "}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {}, clear=True):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "nan"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "inf"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "-inf"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "0"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "-5.0"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 8.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "12.5"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 12.5

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "14"}):
        assert _get_float_env("TEST_FLOAT_VAL", 8.0) == 14.0


@pytest.mark.asyncio
async def test_async_discovery_wrappers():
    """Verify async wrappers discover_active_market_async, is_market_active_async, check_market_status_async."""
    from utils.market_discovery import (
        discover_active_market_async,
        is_market_active_async,
        check_market_status_async,
    )
    with patch("utils.market_discovery.discover_active_market", return_value="KXNFLGAME-TEST") as mock_disc, \
         patch("utils.market_discovery.check_market_status", return_value="open"):
        res = await discover_active_market_async(target_preference="NFL", max_expiration_days=8.0, max_targeted_series_fallbacks=3)
        assert res == "KXNFLGAME-TEST"
        mock_disc.assert_called_once()

        is_active = await is_market_active_async("KXNFLGAME-TEST")
        assert is_active is True

        status = await check_market_status_async("KXNFLGAME-TEST")
        assert status == "open"


def test_targeted_series_fallback_exception_and_cached_short_circuit():
    """Verify _ensure_series_markets handles fetch exceptions and caches already queried series."""
    call_counts = {}

    def mock_fetch(limit=1000, series_ticker=None, max_expiration_days=None):
        if series_ticker:
            call_counts[series_ticker] = call_counts.get(series_ticker, 0) + 1
            if series_ticker == "KXNFLGAME":
                raise RuntimeError("Simulated network timeout")
            return []
        return [{"ticker": "KXNFL-26SEP14-KC", "status": "open"}]

    with patch("utils.market_discovery.fetch_eligible_markets", side_effect=mock_fetch), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.SportsSeasonRouter.get_props_for_league", return_value=["KXNFLGAME"]):

        selected = discover_active_market(target_preference="NFL", preflight_check=True)
        # KXNFLGAME should only have been fetched via network once despite being checked in Tier 1A and Tier 2
        assert call_counts.get("KXNFLGAME") == 1
        # Fallback to Tier 3 general league market succeeds
        assert selected == "KXNFL-26SEP14-KC"


def test_discover_exhausted_probe_budget_breaks_early():
    """Verify that when probe budget is exhausted, discovery breaks out of tier loops cleanly."""
    mock_markets = [{"ticker": "KXNFL-TEST", "status": "open"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]):
        selected = discover_active_market(target_preference="NFL", max_total_probes=0)
        assert selected is None


def test_tier_loops_do_not_break_on_exhausted_probes_when_preflight_disabled():
    """Verify that when preflight_check is False, probe budget does not short-circuit tier loops."""
    # Tier 1A: Primary Moneyline
    mock_markets_1a = [{"ticker": "KXNFLGAME-26SEP14-KC", "series_ticker": "KXNFLGAME", "status": "open"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets_1a), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes") as mock_probe:
        selected = discover_active_market(target_preference="NFL", preflight_check=False, max_total_probes=0)
        assert selected == "KXNFLGAME-26SEP14-KC"
        mock_probe.assert_not_called()

    # Tier 1B: Secondary Game Line
    mock_markets_1b = [{"ticker": "KXNFLSPREAD-26SEP14-KC", "series_ticker": "KXNFLSPREAD", "status": "open"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets_1b), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes") as mock_probe:
        selected = discover_active_market(target_preference="NFL", preflight_check=False, max_total_probes=0)
        assert selected == "KXNFLSPREAD-26SEP14-KC"
        mock_probe.assert_not_called()

    # Tier 2: Player Prop
    mock_markets_tier2 = [{"ticker": "KXNFLTD-26SEP14-MAHOMES", "series_ticker": "KXNFLTD", "status": "open"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets_tier2), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes") as mock_probe:
        selected = discover_active_market(target_preference="NFL", preflight_check=False, max_total_probes=0)
        assert selected == "KXNFLTD-26SEP14-MAHOMES"
        mock_probe.assert_not_called()

    # Tier 3: General League Market
    mock_markets_tier3 = [{"ticker": "KXNFL-26SEP14-KC", "status": "open"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets_tier3), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes") as mock_probe:
        selected = discover_active_market(target_preference="NFL", preflight_check=False, max_total_probes=0)
        assert selected == "KXNFL-26SEP14-KC"
        mock_probe.assert_not_called()


def test_targeted_fallbacks_allowed_when_preflight_disabled_and_probes_zero():
    """Verify targeted series fallback queries succeed when preflight_check is False and max_total_probes is 0."""
    fallback_called = []

    def mock_fetch(limit=1000, series_ticker=None, max_expiration_days=None):
        if series_ticker:
            fallback_called.append(series_ticker)
            if series_ticker == "KXNFLGAME":
                return [{"ticker": "KXNFLGAME-26SEP14-KC", "series_ticker": "KXNFLGAME", "status": "open"}]
            return []
        return [{"ticker": "POLITICS-OTHER", "status": "open"}]

    with patch("utils.market_discovery.fetch_eligible_markets", side_effect=mock_fetch), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes") as mock_probe:
        selected = discover_active_market(
            target_preference="NFL",
            preflight_check=False,
            max_total_probes=0,
            max_targeted_series_fallbacks=2,
            max_expiration_days=8.0,
        )
        # Targeted series fetch for KXNFLGAME should have executed
        assert "KXNFLGAME" in fallback_called
        # No orderbook probing should have occurred
        mock_probe.assert_not_called()
        # Candidate should have been selected directly by in-memory rank
        assert selected == "KXNFLGAME-26SEP14-KC"

    # Also verify that setting max_targeted_series_fallbacks=0 completely stops targeted fallback queries
    fallback_called.clear()
    with patch("utils.market_discovery.fetch_eligible_markets", side_effect=mock_fetch), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]):
        selected = discover_active_market(
            target_preference="NFL",
            preflight_check=False,
            max_total_probes=0,
            max_targeted_series_fallbacks=0,
            max_expiration_days=8.0,
        )
        assert len(fallback_called) == 0
        assert selected is None


def test_discover_non_sports_keyword_bypasses_horizon_filter():
    """
    Verify that non-sports keyword targeting (e.g. FED, CPI, INX) matches and selects
    legitimate contracts expiring beyond the sports weekly horizon.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    distant_close = (now + datetime.timedelta(days=45)).isoformat()

    mock_markets = [
        {
            "ticker": "KXFED-26NOV-CUT25",
            "title": "Federal Reserve Interest Rate Decision November 2026",
            "status": "open",
            "close_time": distant_close,
            "volume_fp": "250000.00",
        },
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        selected = discover_active_market(
            target_preference="FED",
            preflight_check=True,
            max_expiration_days=8.0,
        )
        assert selected == "KXFED-26NOV-CUT25"


def test_parse_float_handles_type_and_value_errors():
    """Verify _parse_float handles invalid types and non-numeric strings."""
    from utils.horizon import _parse_float
    assert _parse_float("not_a_number") == 0.0
    assert _parse_float({}) == 0.0
    assert _parse_float([1, 2, 3]) == 0.0


def test_market_scoring_missing_coverage_branches():
    """Verify market scoring fallback checker, horizon brackets, empty candidates, and empty tickers."""
    import sys
    from utils.market_scoring import _liquidity_key, _select_best_market, _get_orderbook_checker

    # 1. Test _get_orderbook_checker fallback when utils.market_discovery is not in sys.modules
    saved_md = sys.modules.pop("utils.market_discovery", None)
    try:
        checker = _get_orderbook_checker()
        assert callable(checker)
    finally:
        if saved_md is not None:
            sys.modules["utils.market_discovery"] = saved_md

    # 2. Test _liquidity_key horizon brackets (10d -> 2.0x, 25d -> 1.5x, 100d -> 0.5x)
    now = datetime.datetime.now(datetime.timezone.utc)
    m_10d = {"ticker": "KXNFL-10D", "close_time": (now + datetime.timedelta(days=10)).isoformat(), "volume_fp": "100.0"}
    m_25d = {"ticker": "KXNFL-25D", "close_time": (now + datetime.timedelta(days=25)).isoformat(), "volume_fp": "100.0"}
    m_100d = {"ticker": "KXNFL-100D", "close_time": (now + datetime.timedelta(days=100)).isoformat(), "volume_fp": "100.0"}
    assert _liquidity_key(m_10d) > _liquidity_key(m_25d) > _liquidity_key(m_100d)

    # 3. Empty candidates returns None
    assert _select_best_market([]) is None

    # 4. Exhausted budget returns None
    assert _select_best_market([{"ticker": "T1"}], budget_tracker={"remaining": 0}) is None

    # 5. Candidate with empty ticker is skipped
    with patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        res = _select_best_market([{"ticker": ""}, {"ticker": "VALID"}], preflight_check=True)
        assert res == "VALID"


def test_market_api_missing_coverage_branches():
    """Verify market API series filtering, non-200 responses, observed statuses warning, and errors."""
    from utils.market_api import fetch_eligible_markets, check_market_status

    # 1. /events query with series_ticker
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "events": [{"markets": [{"ticker": "KXNFLGAME-1", "status": "open"}]}]
        }
        mock_get.return_value = mock_resp
        res = fetch_eligible_markets(series_ticker="KXNFLGAME")
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
        res = fetch_eligible_markets(series_ticker="KXNFLGAME")
        assert len(res) == 1
        assert res[0]["ticker"] == "KXNFLGAME-FALLBACK"

    # 3. All returned markets filtered out triggers observed statuses warning
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"markets": [{"ticker": "KX-CLOSED", "status": "closed"}]}
        mock_get.return_value = mock_resp
        res = fetch_eligible_markets()
        assert res == []

    # 4. Top-level exception in fetch_eligible_markets returns []
    with patch("requests.get", side_effect=RuntimeError("Fatal error")):
        assert fetch_eligible_markets() == []

    # 5. Non-200 in check_market_status returns None
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp
        assert check_market_status("INVALID") is None


def test_market_discovery_missing_coverage_branches():
    """Verify tradeable market absence, NBA preferences, non-preflight exact matches, and tier 2 budget exhaustion."""
    # 1. No tradeable markets available returns None
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=[]):
        assert discover_active_market() is None

    # 2. Target preference for NBA
    mock_nba = [{"ticker": "KXNBAGAME-1", "series_ticker": "KXNBAGAME", "status": "open", "volume_fp": "100.0"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_nba), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        res = discover_active_market(target_preference="NBA")
        assert res == "KXNBAGAME-1"

    # 3. Exact match with preflight_check=False
    mock_exact = [{"ticker": "KXNFL-SPECIFIC", "status": "open"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_exact):
        res = discover_active_market(target_preference="KXNFL-SPECIFIC", preflight_check=False)
        assert res == "KXNFL-SPECIFIC"

    # 4. Excluded exact NBA and MLB target routes to league suite
    dummy_pool = [{"ticker": "KXOTHER-1", "status": "open"}]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=dummy_pool), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=False):
        assert discover_active_market(target_preference="KXNBAGAME-EX", exclude_tickers=["KXNBAGAME-EX"]) is None
        assert discover_active_market(target_preference="KXMLBGAME-EX", exclude_tickers=["KXMLBGAME-EX"]) is None

    # 5. Exhausted probes inside Tier 2 props inner loop breaks
    mock_props = [
        {"ticker": "KXNFLTD-1", "series_ticker": "KXNFLTD", "status": "open", "volume_fp": "10.0"},
        {"ticker": "KXNFLPASSYDS-1", "series_ticker": "KXNFLPASSYDS", "status": "open", "volume_fp": "10.0"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_props), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=False):
        res = discover_active_market(target_preference="SPORTS", max_total_probes=4)
        assert res is None


@pytest.mark.asyncio
async def test_is_market_active_honors_check_market_status_override():
    """Verify is_market_active and is_market_active_async consult check_market_status on market_discovery."""
    import sys
    from utils.market_discovery import is_market_active, is_market_active_async
    import utils.market_api as ma

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


def test_is_within_horizon_clock_seam_fallback():
    """Verify _is_within_horizon honors patched clock on utils.market_discovery and standalone fallback."""
    import sys
    from utils.horizon import _is_within_horizon

    m = {"ticker": "KXTEST-1", "close_time": "2026-10-18T00:00:00Z"}
    mock_now = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc)

    # 1. Honors mocked datetime on utils.market_discovery
    with patch("utils.market_discovery.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = mock_now
        mock_dt.datetime.timezone = datetime.timezone
        assert _is_within_horizon(m, max_days=7.0) is True

    # 2. Standalone fallback when utils.market_discovery is not in sys.modules
    saved_md = sys.modules.pop("utils.market_discovery", None)
    try:
        assert _is_within_horizon({"ticker": "KXTEST-2"}, max_days=7.0) is True
    finally:
        if saved_md is not None:
            sys.modules["utils.market_discovery"] = saved_md


def test_legacy_patch_import_attributes_compatibility():
    """Verify legacy patch/import seams for BASE_URL, requests, and certifi are preserved."""
    import sys
    import certifi
    import requests
    from utils import market_api as ma
    from utils import market_discovery as md

    # 1. Imports from utils.market_discovery
    from utils.market_discovery import BASE_URL as MD_BASE_URL, requests as md_requests, certifi as md_certifi
    assert MD_BASE_URL == "https://api.elections.kalshi.com"
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
        assert ma._get_base_url() == "https://api.elections.kalshi.com"
        assert ma._get_requests() is requests
        assert ma._get_certifi() is certifi
    finally:
        if saved_md is not None:
            sys.modules["utils.market_discovery"] = saved_md















