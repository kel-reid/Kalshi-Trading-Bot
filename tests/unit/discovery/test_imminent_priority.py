"""
Unit Tests for Imminent Window Cross-League Priority and ESPN Live Schedule Elevation
"""

import datetime
from unittest.mock import patch, MagicMock

import pytest

from utils.market_discovery import (
    discover_active_market,
    SportsSeasonRouter,
)
from utils.espn_client import clear_slate_cache


@pytest.fixture(autouse=True)
def reset_cache():
    clear_slate_cache()
    yield
    clear_slate_cache()


def test_sports_season_router_elevates_league_with_live_games_today():
    """Verify SportsSeasonRouter dynamically elevates leagues with live ESPN games today."""
    # Wednesday Oct 21, 2026: Base calendar is ["NFL", "NBA", "NCAAF", "MLB"]
    dt_wed = datetime.datetime(2026, 10, 21, 18, 0, tzinfo=datetime.timezone.utc)

    # Mock ESPN counts: NBA has 11 games, NFL has 0, NCAAF has 0, MLB has 0
    mock_counts = {"NBA": 11, "NFL": 0, "NCAAF": 0, "MLB": 0}

    with patch("utils.espn_client.get_daily_slate_counts", return_value=mock_counts):
        leagues = SportsSeasonRouter.get_in_season_leagues(dt_wed, use_live_schedule=True)
        # NBA must be elevated to Priority #1
        assert leagues[0] == "NBA"
        assert leagues == ["NBA", "NFL", "NCAAF", "MLB"]


def test_sports_season_router_maintains_cfb_priority_on_saturday_when_both_have_games():
    """Verify on Saturday, if both NCAAF and NBA have games, NCAAF maintains #1 priority."""
    # Saturday Oct 24, 2026
    dt_sat = datetime.datetime(2026, 10, 24, 18, 0, tzinfo=datetime.timezone.utc)

    mock_counts = {"NCAAF": 45, "NBA": 8, "NFL": 0, "MLB": 0}

    with patch("utils.espn_client.get_daily_slate_counts", return_value=mock_counts):
        leagues = SportsSeasonRouter.get_in_season_leagues(dt_sat, use_live_schedule=True)
        assert leagues[0] == "NCAAF"
        assert leagues[1] == "NBA"
        assert leagues == ["NCAAF", "NBA", "NFL", "MLB"]


def test_sports_season_router_prioritizes_nba_over_cfb_on_weekdays_when_both_have_games():
    """Verify on non-primetime weekdays (e.g. Tuesday), NBA takes priority over NCAAF when both have games."""
    # Tuesday Oct 27, 2026
    dt_tue = datetime.datetime(2026, 10, 27, 18, 0, tzinfo=datetime.timezone.utc)

    # Tuesday slate: NBA has 4 games, NCAAF has 1 game, NFL has 0, MLB has 0
    mock_counts = {"NBA": 4, "NCAAF": 1, "NFL": 0, "MLB": 0}

    with patch("utils.espn_client.get_daily_slate_counts", return_value=mock_counts):
        leagues = SportsSeasonRouter.get_in_season_leagues(dt_tue, use_live_schedule=True)
        assert leagues[0] == "NBA"
        assert leagues[1] == "NCAAF"
        assert leagues == ["NBA", "NCAAF", "NFL", "MLB"]


def test_sports_season_router_keeps_nba_ahead_of_cfb_when_cfb_has_more_games():
    """Verify calendar priority holds on a non-primetime weekday even when NCAAF has more games than NBA."""
    dt_tue = datetime.datetime(2026, 10, 27, 18, 0, tzinfo=datetime.timezone.utc)
    mock_counts = {"NBA": 1, "NCAAF": 4, "NFL": 0, "MLB": 0}

    with patch("utils.espn_client.get_daily_slate_counts", return_value=mock_counts):
        leagues = SportsSeasonRouter.get_in_season_leagues(dt_tue, use_live_schedule=True)
        assert leagues[0] == "NBA"
        assert leagues[1] == "NCAAF"
        assert leagues == ["NBA", "NCAAF", "NFL", "MLB"]


def test_sports_season_router_falls_back_to_calendar_matrix_when_espn_fails():
    """Verify SportsSeasonRouter falls back cleanly to calendar matrix if ESPN raises or returns empty."""
    dt_wed = datetime.datetime(2026, 10, 21, 18, 0, tzinfo=datetime.timezone.utc)

    with patch("utils.espn_client.get_daily_slate_counts", side_effect=Exception("ESPN API Timeout")):
        leagues = SportsSeasonRouter.get_in_season_leagues(dt_wed, use_live_schedule=True)
        assert leagues == ["NFL", "NBA", "NCAAF", "MLB"]


def test_discover_active_market_prefers_imminent_game_over_distant_higher_priority_league():
    """
    Verify that discover_active_market selects an imminent game (<= 18h away) in a lower-priority
    league over a distant game (> 18h away) in a higher-priority league.
    """
    # Now is 2026-10-21 18:00 UTC
    now_utc = datetime.datetime(2026, 10, 21, 18, 0, tzinfo=datetime.timezone.utc)

    # NFL game is 28 hours away (Oct 22 22:00 UTC)
    nfl_close = "2026-10-22T22:00:00Z"
    # NBA game is 3 hours away (Oct 21 21:00 UTC)
    nba_close = "2026-10-21T21:00:00Z"

    mock_markets = [
        {
            "ticker": "KXNFLGAME-26OCT22NECHI-NE",
            "series_ticker": "KXNFLGAME",
            "status": "open",
            "volume_fp": "50000.00",
            "close_time": nfl_close,
        },
        {
            "ticker": "KXNBAGAME-26OCT21ATLORL-ATL",
            "series_ticker": "KXNBAGAME",
            "status": "open",
            "volume_fp": "45000.00",
            "close_time": nba_close,
        },
    ]

    with patch("utils.market_discovery.fetch_eligible_markets", return_value=mock_markets), \
         patch("utils.market_discovery.check_orderbook_has_quotes", return_value=True), \
         patch("utils.sports_router.SportsSeasonRouter.get_in_season_leagues", return_value=["NFL", "NBA"]), \
         patch("utils.market_discovery.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = now_utc
        mock_dt.timezone = datetime.timezone
        mock_dt.timedelta = datetime.timedelta

        selected = discover_active_market(target_preference="SPORTS", preflight_check=True)

        # Must select the imminent NBA game (starts in 3h) rather than the NFL game (starts in 28h)
        assert selected == "KXNBAGAME-26OCT21ATLORL-ATL"


def test_sports_season_router_preserves_base_order_on_partial_failure():
    """Verify SportsSeasonRouter preserves standard calendar order if any league fails (returns None)."""
    dt_wed = datetime.datetime(2026, 10, 21, 18, 0, tzinfo=datetime.timezone.utc)
    # NBA succeeds with 11 games, but NFL failed (None)
    mock_partial = {"NBA": 11, "NFL": None, "NCAAF": 0, "MLB": 0}

    with patch("utils.espn_client.get_daily_slate_counts", return_value=mock_partial):
        leagues = SportsSeasonRouter.get_in_season_leagues(dt_wed, use_live_schedule=True)
        # Must preserve original calendar order without demoting NFL
        assert leagues == ["NFL", "NBA", "NCAAF", "MLB"]


def test_select_best_market_avoids_duplicate_probes():
    """Verify _select_best_market skips tickers already recorded in budget_tracker['probed_tickers']."""
    from utils.market_scoring import _select_best_market

    candidates = [
        {"ticker": "MKT-1", "volume_fp": "100.0"},
        {"ticker": "MKT-2", "volume_fp": "50.0"},
    ]

    budget_tracker = {
        "remaining": 5,
        "probed_tickers": {"MKT-1"},  # MKT-1 already probed in a previous phase
    }

    mock_checker = MagicMock(return_value=True)
    with patch("utils.market_scoring._get_orderbook_checker", return_value=mock_checker):
        selected = _select_best_market(candidates, preflight_check=True, budget_tracker=budget_tracker)
        # MKT-1 must be skipped, MKT-2 probed and selected
        assert selected == "MKT-2"
        mock_checker.assert_called_once_with("MKT-2", min_mid_price=None, max_mid_price=None)
        assert "MKT-2" in budget_tracker["probed_tickers"]
        assert budget_tracker["remaining"] == 4

