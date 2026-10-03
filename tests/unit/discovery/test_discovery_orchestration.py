"""
Unit Tests for Market Discovery Orchestration and Cascading

Tests top-level contract discovery:
- Exact ticker matching and exclusion
- Sports vs non-sports keyword matching across titles and tickers
- Discovery wide probe budget caps and per-series probe bounds
- Targeted series fallback deduplication, caching, and network short-circuits
- Tier loop progression with preflight enabled vs disabled
- Configuration environment variable defensive parsing and validation
- Async discovery wrappers
"""

import datetime
import os
import pytest
from unittest.mock import patch, MagicMock

from config import _get_float_env, _get_int_env
from utils.market_discovery import (
    discover_active_market,
    discover_active_market_async,
    is_market_active_async,
    check_market_status_async,
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
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch, \
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL", "NBA", "MLB"]):
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
         patch("utils.market_discovery.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL", "NBA", "MLB"]), \
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
        for i in range(30)
    ]

    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=False) as mock_probe:
        result = discover_active_market(target_preference="NFL", max_total_probes=35)
        assert result is None
        # KXNFLGAME had 30 candidates, but per-series limit is 25; subsequent series had 0
        assert mock_probe.call_count == 25



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

    # allow_zero=True tests
    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "0"}):
        assert _get_float_env("TEST_FLOAT_VAL", 3.0, allow_zero=True) == 0.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "0.0"}):
        assert _get_float_env("TEST_FLOAT_VAL", 3.0, allow_zero=True) == 0.0

    with patch.dict(os.environ, {"TEST_FLOAT_VAL": "-1.0"}):
        assert _get_float_env("TEST_FLOAT_VAL", 3.0, allow_zero=True) == 3.0


def test_config_get_int_env_fail_fast_validation():
    """Verify _get_int_env in config returns default for missing/empty and raises ValueError on invalid values."""
    # Missing or empty falls back to default
    with patch.dict(os.environ, {}, clear=True):
        assert _get_int_env("TEST_INT_VAL", 100) == 100

    with patch.dict(os.environ, {"TEST_INT_VAL": ""}):
        assert _get_int_env("TEST_INT_VAL", 100) == 100

    with patch.dict(os.environ, {"TEST_INT_VAL": "   "}):
        assert _get_int_env("TEST_INT_VAL", 100) == 100

    # Valid positive integers
    with patch.dict(os.environ, {"TEST_INT_VAL": "50"}):
        assert _get_int_env("TEST_INT_VAL", 100) == 50

    with patch.dict(os.environ, {"TEST_INT_VAL": "  250  "}):
        assert _get_int_env("TEST_INT_VAL", 100) == 250

    # Invalid non-integer or float formats raise ValueError
    with pytest.raises(ValueError, match="TEST_INT_VAL must be a positive integer"):
        with patch.dict(os.environ, {"TEST_INT_VAL": "10.0"}):
            _get_int_env("TEST_INT_VAL", 100)

    with pytest.raises(ValueError, match="TEST_INT_VAL must be a positive integer"):
        with patch.dict(os.environ, {"TEST_INT_VAL": "invalid_number"}):
            _get_int_env("TEST_INT_VAL", 100)

    # Zero or negative integers raise ValueError
    with pytest.raises(ValueError, match="TEST_INT_VAL must be a positive integer"):
        with patch.dict(os.environ, {"TEST_INT_VAL": "0"}):
            _get_int_env("TEST_INT_VAL", 100)

    with pytest.raises(ValueError, match="TEST_INT_VAL must be a positive integer"):
        with patch.dict(os.environ, {"TEST_INT_VAL": "-5"}):
            _get_int_env("TEST_INT_VAL", 100)


@pytest.mark.asyncio
async def test_async_discovery_wrappers():
    """Verify async wrappers discover_active_market_async, is_market_active_async, check_market_status_async."""
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
