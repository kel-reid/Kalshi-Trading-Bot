"""
Unit Tests for ESPN Daily Slate Scanner Client
"""

import datetime
from unittest.mock import patch, MagicMock

import pytest

from utils.espn_client import (
    clear_slate_cache,
    fetch_espn_league_games_today,
    get_daily_slate_counts,
    ET_TZ,
)


@pytest.fixture(autouse=True)
def reset_cache():
    clear_slate_cache()
    yield
    clear_slate_cache()


def test_fetch_espn_league_games_today_parses_events():
    """Verify fetch_espn_league_games_today correctly counts active scheduled events for target date in ET."""
    target_date = datetime.date(2026, 10, 21)
    mock_payload = {
        "events": [
            {
                "name": "Atlanta Hawks at Orlando Magic",
                "date": "2026-10-21T23:00Z",  # 19:00 EDT on 2026-10-21
                "status": {"type": {"name": "STATUS_SCHEDULED", "description": "Scheduled"}},
            },
            {
                "name": "Milwaukee Bucks at Washington Wizards",
                "date": "2026-10-21T23:00Z",
                "status": {"type": {"name": "STATUS_IN_PROGRESS", "description": "In Progress"}},
            },
            {
                "name": "Golden State Warriors at Los Angeles Lakers",
                "date": "2026-10-22T02:30Z",  # 22:30 EDT on 2026-10-21 (still Oct 21 in ET!)
                "status": {"type": {"name": "STATUS_SCHEDULED", "description": "Scheduled"}},
            },
        ]
    }

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = mock_payload

    with patch("utils.espn_client.requests.get", return_value=mock_resp) as mock_get:
        count = fetch_espn_league_games_today("NBA", target_date)
        assert count == 3
        mock_get.assert_called_once()
        assert mock_get.call_args[1]["params"] == {"dates": "20261021"}


def test_fetch_espn_league_games_today_filters_postponed_and_wrong_date():
    """Verify cancelled, postponed, or wrong-date games are excluded from count."""
    target_date = datetime.date(2026, 10, 21)
    mock_payload = {
        "events": [
            {
                "name": "Active Game Today",
                "date": "2026-10-21T23:00Z",
                "status": {"type": {"name": "STATUS_SCHEDULED"}},
            },
            {
                "name": "Game Tomorrow",
                "date": "2026-10-22T23:00Z",  # Tomorrow
                "status": {"type": {"name": "STATUS_SCHEDULED"}},
            },
            {
                "name": "Postponed Game",
                "date": "2026-10-21T23:00Z",
                "status": {"type": {"name": "STATUS_POSTPONED"}},
            },
            {
                "name": "Canceled Game",
                "date": "2026-10-21T23:00Z",
                "status": {"type": {"name": "STATUS_CANCELED"}},
            },
        ]
    }

    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = mock_payload

    with patch("utils.espn_client.requests.get", return_value=mock_resp):
        count = fetch_espn_league_games_today("NBA", target_date)
        assert count == 1


def test_fetch_espn_league_games_today_handles_network_error_gracefully():
    """Verify network error or non-200 status code returns None without crashing."""
    target_date = datetime.date(2026, 10, 21)

    with patch("utils.espn_client.requests.get", side_effect=Exception("Connection reset")):
        count = fetch_espn_league_games_today("NBA", target_date)
        assert count is None

    mock_500 = MagicMock(status_code=500)
    with patch("utils.espn_client.requests.get", return_value=mock_500):
        count = fetch_espn_league_games_today("NBA", target_date)
        assert count is None

    # Unknown league
    assert fetch_espn_league_games_today("UNKNOWN", target_date) is None


def test_get_daily_slate_counts_caches_results():
    """Verify get_daily_slate_counts caches counts for the same date and TTL."""
    test_dt = datetime.datetime(2026, 10, 21, 19, 0, tzinfo=datetime.timezone.utc)

    with patch("utils.espn_client.fetch_espn_league_games_today") as mock_fetch:
        mock_fetch.side_effect = lambda league, date: 11 if league == "NBA" else 0

        # First call queries fetch_espn_league_games_today
        counts1 = get_daily_slate_counts(dt=test_dt, leagues=["NBA", "NFL"])
        assert counts1 == {"NBA": 11, "NFL": 0}
        assert mock_fetch.call_count == 2

        # Second call within TTL hits in-memory cache without calling fetch again
        counts2 = get_daily_slate_counts(dt=test_dt, leagues=["NBA", "NFL"])
        assert counts2 == {"NBA": 11, "NFL": 0}
        assert mock_fetch.call_count == 2


def test_get_daily_slate_counts_merges_missing_leagues_into_cache():
    """Verify requesting a subset first and superset second fetches and merges missing leagues."""
    test_dt = datetime.datetime(2026, 10, 21, 19, 0, tzinfo=datetime.timezone.utc)

    with patch("utils.espn_client.fetch_espn_league_games_today") as mock_fetch:
        mock_fetch.side_effect = lambda league, date: 11 if league == "NBA" else 2

        # First call queries only NBA
        counts1 = get_daily_slate_counts(dt=test_dt, leagues=["NBA"])
        assert counts1 == {"NBA": 11}
        assert mock_fetch.call_count == 1

        # Second call queries NBA and NFL; only NFL should be fetched
        counts2 = get_daily_slate_counts(dt=test_dt, leagues=["NBA", "NFL"])
        assert counts2 == {"NBA": 11, "NFL": 2}
        assert mock_fetch.call_count == 2


def test_get_daily_slate_counts_does_not_cache_partial_failures():
    """Verify that if one league fails (returns None), the partial result is returned without being cached."""
    test_dt = datetime.datetime(2026, 10, 21, 19, 0, tzinfo=datetime.timezone.utc)

    with patch("utils.espn_client.fetch_espn_league_games_today") as mock_fetch:
        # NBA succeeds, NFL fails
        mock_fetch.side_effect = lambda league, date: 11 if league == "NBA" else None

        counts = get_daily_slate_counts(dt=test_dt, leagues=["NBA", "NFL"])
        assert counts == {"NBA": 11, "NFL": None}

        # Since it had a failure, cache should not hold failed data; next call re-attempts fetch
        mock_fetch.side_effect = lambda league, date: 11 if league == "NBA" else 1
        counts_retry = get_daily_slate_counts(dt=test_dt, leagues=["NBA", "NFL"])
        assert counts_retry == {"NBA": 11, "NFL": 1}

