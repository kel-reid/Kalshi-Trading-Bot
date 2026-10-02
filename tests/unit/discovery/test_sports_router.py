"""
Unit Tests for Sports Season Router and Multi-League Cascading

Tests seasonal league priorities (NFL, NBA, MLB), series product suites,
tier 1A moneyline probing order, and excluded exact ticker rotation fallbacks.
"""

import datetime
from unittest.mock import patch, MagicMock

from utils.market_discovery import (
    discover_active_market,
    fetch_eligible_markets,
    SportsSeasonRouter,
)


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
    """Verify SportsSeasonRouter resolves seasonal league priorities correctly by month during standard days."""
    # Early Fall: September (month 9, Tuesday) -> NFL, NCAAF, MLB (NBA not started)
    dt_sep = datetime.datetime(2026, 9, 15, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_sep) == ["NFL", "NCAAF", "MLB"]

    # Fall/Winter: October (month 10, Thursday) -> NFL, NCAAF, NBA, MLB
    dt_oct = datetime.datetime(2026, 10, 15, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_oct) == ["NFL", "NCAAF", "NBA", "MLB"]

    # Late Fall / Winter: November & December (Sunday & Tuesday) -> NFL, NCAAF, NBA
    dt_nov = datetime.datetime(2026, 11, 15, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_nov) == ["NFL", "NCAAF", "NBA"]
    dt_dec = datetime.datetime(2026, 12, 15, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_dec) == ["NFL", "NCAAF", "NBA"]

    # Mid-Winter: January (month 1, Wednesday) -> NFL, NCAAF, NBA
    dt_jan = datetime.datetime(2026, 1, 14, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_jan) == ["NFL", "NCAAF", "NBA"]

    # Post-Season: February (month 2) -> NFL, NBA (NCAAF concluded)
    dt_feb = datetime.datetime(2026, 2, 10, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_feb) == ["NFL", "NBA"]

    # Spring: April (month 4) -> NBA, MLB
    dt_apr = datetime.datetime(2026, 4, 15, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_apr) == ["NBA", "MLB"]

    # Summer Lull: July (month 7) -> MLB
    dt_jul = datetime.datetime(2026, 7, 15, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_jul) == ["MLB"]

    # Late Summer: August (month 8) -> MLB only (NFL/NCAAF preseason excluded)
    dt_aug = datetime.datetime(2026, 8, 15, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_aug) == ["MLB"]


def test_sports_season_router_full_product_suites():
    """Verify in-season series include Game Lines and Player Props for all major leagues during active overlap."""
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
    # NCAAF Game Lines
    assert "KXNCAAFGAME" in series
    assert "KXNCAAFSPREAD" in series
    assert "KXNCAAFTOTAL" in series
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

    ncaaf_suite = SportsSeasonRouter.get_series_for_league("NCAAF")
    assert "KXNCAAFGAME" in ncaaf_suite
    assert "KXNCAAFSPREAD" in ncaaf_suite
    assert "KXNCAAFTOTAL" in ncaaf_suite

    cfb_suite = SportsSeasonRouter.get_series_for_league("CFB")
    assert cfb_suite == ncaaf_suite
    assert SportsSeasonRouter.get_primary_series_for_league("NCAAF") == "KXNCAAFGAME"
    assert SportsSeasonRouter.get_primary_series_for_league("CFB") == "KXNCAAFGAME"
    assert "KXNCAAF" in SportsSeasonRouter.ALL_IN_SEASON_PREFIXES

    mlb_suite = SportsSeasonRouter.get_series_for_league("MLB")
    assert "KXMLBGAME" in mlb_suite
    assert "KXMLBSPREAD" in mlb_suite
    assert "KXMLBKS" in mlb_suite
    assert "KXMLBHIT" in mlb_suite
    assert "KXMLBTB" in mlb_suite


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


def test_discover_active_market_prioritizes_cfb_and_ncaaf_preferences():
    """Verify discover_active_market routes CFB, NCAAF, and COLLEGE FOOTBALL target preferences to KXNCAAFGAME."""
    mock_markets = [
        {"ticker": "KXNFLGAME-26OCT04KC", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "80000.00"},
        {"ticker": "KXNCAAFGAME-26OCT03TEXOU", "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "50000.00"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        # Targeting "CFB"
        result_cfb = discover_active_market(target_preference="CFB")
        assert result_cfb == "KXNCAAFGAME-26OCT03TEXOU"

        # Targeting "NCAAF"
        result_ncaaf = discover_active_market(target_preference="NCAAF")
        assert result_ncaaf == "KXNCAAFGAME-26OCT03TEXOU"

        # Targeting "COLLEGE FOOTBALL"
        result_cf = discover_active_market(target_preference="COLLEGE FOOTBALL")
        assert result_cf == "KXNCAAFGAME-26OCT03TEXOU"


def test_excluded_exact_cfb_ticker_rotates_to_ncaaf_suite():
    """
    Verify that when an exact CFB / NCAAF ticker is excluded during auto-rotation,
    discovery routes to the NCAAF suite to find an active replacement game line.
    """
    mock_markets = [
        {"ticker": "KXNCAAFGAME-OLD-SETTLED", "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "60000.00"},
        {"ticker": "KXNCAAFGAME-NEW-ACTIVE", "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "45000.00"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        selected = discover_active_market(
            target_preference="KXNCAAFGAME-OLD-SETTLED",
            exclude_tickers=["KXNCAAFGAME-OLD-SETTLED"],
            preflight_check=True,
        )
        assert selected == "KXNCAAFGAME-NEW-ACTIVE"


def test_tier_1a_probes_ncaaf_primary_moneyline_when_nfl_dormant():
    """
    Verify that during fall overlap (e.g. October), when NFL moneyline is dormant,
    Tier 1A probes NCAAF moneyline (KXNCAAFGAME) and selects it before secondary spread lines.
    """
    mock_markets = [
        {"ticker": "KXNFLGAME-OCT-DORMANT", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "10000.00"},
        {"ticker": "KXNFLSPREAD-OCT-ACTIVE", "series_ticker": "KXNFLSPREAD", "status": "open", "volume_fp": "15000.00"},
        {"ticker": "KXNCAAFGAME-OCT-ACTIVE", "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "20000.00"},
    ]

    def mock_quotes(ticker: str) -> bool:
        return "KXNCAAFGAME" in ticker or "KXNFLSPREAD" in ticker

    dt_oct = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc)
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL", "NCAAF", "NBA", "MLB"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes", side_effect=mock_quotes) as mock_probe:
        selected = discover_active_market(target_preference="SPORTS", preflight_check=True)
        assert selected == "KXNCAAFGAME-OCT-ACTIVE"

        probed_tickers = [call.args[0] for call in mock_probe.call_args_list]
        assert "KXNFLGAME-OCT-DORMANT" in probed_tickers
        assert "KXNCAAFGAME-OCT-ACTIVE" in probed_tickers
        # KXNFLSPREAD must not be probed because KXNCAAFGAME was resolved in Tier 1A
        assert "KXNFLSPREAD-OCT-ACTIVE" not in probed_tickers


def test_unmatched_target_containing_cfb_substring_does_not_route_to_ncaaf():
    """
    Verify that an unmatched target ticker containing 'CFB' only as an embedded substring
    (e.g., 'INX-NONCFB-2026') does NOT erroneously route to the NCAAF suite, but instead
    falls back to the seasonal multi-league router.
    """
    mock_markets = [
        {"ticker": "INX-NONCFB-2026", "status": "open", "volume_fp": "50000.00"},
        {"ticker": "KXNFLGAME-ACTIVE-1", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "20000.00"},
        {"ticker": "KXNCAAFGAME-ACTIVE-1", "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "20000.00"},
    ]
    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL", "NCAAF"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        selected = discover_active_market(
            target_preference="INX-NONCFB-2026",
            exclude_tickers=["INX-NONCFB-2026"],
            preflight_check=True,
        )
        # Should route to seasonal fallback and pick NFL primary moneyline, NOT NCAAF
        assert selected == "KXNFLGAME-ACTIVE-1"


def test_october_quadruple_overlap_queries_mlb_when_nfl_ncaaf_nba_fallbacks_empty():
    """
    Verify that in October's 4-league overlap (NFL, NCAAF, NBA, MLB), when global /events fetch
    omits sports markets and the first three targeted fallback queries (KXNFLGAME, KXNCAAFGAME, KXNBAGAME)
    return empty, discovery preserves a targeted fallback for the fourth league and selects active KXMLBGAME.
    """
    global_markets = [
        {"ticker": "INX-DAILY-26OCT15", "status": "open", "volume_fp": "100000.00"}
    ]
    queried_series_calls = []

    def mock_fetch(limit=1000, series_ticker=None, max_expiration_days=None):
        if series_ticker is None:
            return global_markets
        queried_series_calls.append(series_ticker)
        if series_ticker == "KXMLBGAME":
            return [
                {
                    "ticker": "KXMLBGAME-26OCT15-WS",
                    "series_ticker": "KXMLBGAME",
                    "status": "open",
                    "volume_fp": "45000.00",
                }
            ]
        return []

    dt_oct = datetime.datetime(2026, 10, 15, tzinfo=datetime.timezone.utc)
    with patch("utils.market_discovery.fetch_eligible_markets", side_effect=mock_fetch), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL", "NCAAF", "NBA", "MLB"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = dt_oct
        mock_dt.datetime.timezone = datetime.timezone
        selected = discover_active_market(target_preference="SPORTS", preflight_check=True)

        assert selected == "KXMLBGAME-26OCT15-WS"
        assert queried_series_calls == ["KXNFLGAME", "KXNCAAFGAME", "KXNBAGAME", "KXMLBGAME"]


def test_targeted_discovery_continues_when_global_fetch_only_contains_excluded_ticker():
    """
    Verify that when the initial global fetch returns only the excluded ticker,
    discovery does not prematurely abort with None, but instead continues to targeted
    series queries (e.g., KXNCAAFGAME) to locate an active replacement.
    """
    excluded_ticker = "KXNCAAFGAME-OLD-SETTLED"
    replacement_ticker = "KXNCAAFGAME-NEW-ACTIVE"

    def mock_fetch(limit=1000, series_ticker=None, max_expiration_days=None):
        if series_ticker is None:
            # Global fetch only returns the excluded ticker
            return [
                {"ticker": excluded_ticker, "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "50000.00"}
            ]
        elif series_ticker == "KXNCAAFGAME":
            # Targeted query returns both the old excluded ticker and the new active replacement
            return [
                {"ticker": excluded_ticker, "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "50000.00"},
                {"ticker": replacement_ticker, "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "40000.00"},
            ]
        return []

    with patch("utils.market_discovery.fetch_eligible_markets", side_effect=mock_fetch), \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NCAAF"]), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True):
        selected = discover_active_market(
            target_preference="CFB",
            exclude_tickers=[excluded_ticker],
            preflight_check=True,
        )
        assert selected == replacement_ticker


def test_sports_season_router_day_of_week_football_dynamics():
    """
    Verify that on Friday and Saturday during football season (Sep - Jan),
    NCAAF is elevated to Priority #1 ahead of NFL.
    On Sunday, Monday, and Thursday, NFL retains Priority #1.
    """
    # 1. September:
    # Friday Sep 18, 2026 (18:00 UTC = 14:00 EDT)
    dt_sep_fri = datetime.datetime(2026, 9, 18, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_sep_fri) == ["NCAAF", "NFL", "MLB"]
    # Saturday Sep 19, 2026
    dt_sep_sat = datetime.datetime(2026, 9, 19, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_sep_sat) == ["NCAAF", "NFL", "MLB"]
    # Sunday Sep 20, 2026
    dt_sep_sun = datetime.datetime(2026, 9, 20, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_sep_sun) == ["NFL", "NCAAF", "MLB"]
    # Monday Sep 21, 2026
    dt_sep_mon = datetime.datetime(2026, 9, 21, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_sep_mon) == ["NFL", "NCAAF", "MLB"]
    # Thursday Sep 24, 2026
    dt_sep_thu = datetime.datetime(2026, 9, 24, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_sep_thu) == ["NFL", "NCAAF", "MLB"]

    # 2. October (Quadruple Overlap):
    # Friday Oct 2, 2026
    dt_oct_fri = datetime.datetime(2026, 10, 2, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_oct_fri) == ["NCAAF", "NFL", "NBA", "MLB"]
    # Saturday Oct 3, 2026
    dt_oct_sat = datetime.datetime(2026, 10, 3, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_oct_sat) == ["NCAAF", "NFL", "NBA", "MLB"]
    # Sunday Oct 4, 2026
    dt_oct_sun = datetime.datetime(2026, 10, 4, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_oct_sun) == ["NFL", "NCAAF", "NBA", "MLB"]

    # 3. November / December / January:
    dt_nov_sat = datetime.datetime(2026, 11, 21, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_nov_sat) == ["NCAAF", "NFL", "NBA"]
    dt_nov_sun = datetime.datetime(2026, 11, 22, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_nov_sun) == ["NFL", "NCAAF", "NBA"]

    # 4. Out-of-season / Concluded (e.g. February): Day of week does not change priority
    dt_feb_sat = datetime.datetime(2026, 2, 14, 18, 0, tzinfo=datetime.timezone.utc)
    assert SportsSeasonRouter.get_in_season_leagues(dt_feb_sat) == ["NFL", "NBA"]


def test_sports_router_discovery_prefers_cfb_on_friday_and_saturday():
    """
    Verify that when discovery runs on Friday or Saturday under general sports routing,
    the seasonal waterfall queries and selects NCAAF ahead of NFL.
    """
    mock_markets = [
        {"ticker": "KXNFLGAME-26OCT04KCLV-LV", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "60000.00"},
        {"ticker": "KXNCAAFGAME-26OCT02LIBDEL", "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "30000.00"},
    ]
    # Friday Oct 2, 2026 18:00 UTC (14:00 EDT)
    dt_friday = datetime.datetime(2026, 10, 2, 18, 0, tzinfo=datetime.timezone.utc)

    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = dt_friday
        mock_dt.timezone = datetime.timezone
        mock_dt.timedelta = datetime.timedelta

        selected = discover_active_market(target_preference="SPORTS", preflight_check=True)
        assert selected == "KXNCAAFGAME-26OCT02LIBDEL"


def test_sports_router_discovery_prefers_nfl_on_sunday():
    """
    Verify that when discovery runs on Sunday under general sports routing,
    the seasonal waterfall queries and selects NFL ahead of NCAAF.
    """
    mock_markets = [
        {"ticker": "KXNFLGAME-26OCT04KCLV-LV", "series_ticker": "KXNFLGAME", "status": "open", "volume_fp": "60000.00"},
        {"ticker": "KXNCAAFGAME-26OCT02LIBDEL", "series_ticker": "KXNCAAFGAME", "status": "open", "volume_fp": "30000.00"},
    ]
    # Sunday Oct 4, 2026 18:00 UTC (14:00 EDT)
    dt_sunday = datetime.datetime(2026, 10, 4, 18, 0, tzinfo=datetime.timezone.utc)

    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.market_discovery.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = dt_sunday
        mock_dt.timezone = datetime.timezone
        mock_dt.timedelta = datetime.timedelta

        selected = discover_active_market(target_preference="SPORTS", preflight_check=True)
        assert selected == "KXNFLGAME-26OCT04KCLV-LV"

