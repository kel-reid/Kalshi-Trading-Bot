"""
Unit Tests for Expiration Horizon Filtering and ISO Timestamp Parsing

Tests date filtering mechanisms:
- Weekly horizon bounding (<= 8 days)
- Custom max_expiration_days enforcement
- Exact match and non-sports keyword horizon bypasses
- ISO 8601 parsing (aware, naive, offsets, malformed inputs)
- Fail-closed validation on corrupt timestamps
"""

import datetime
from unittest.mock import patch, MagicMock

from utils.market_discovery import (
    discover_active_market,
    fetch_eligible_markets,
    _parse_iso_timestamp,
    _is_within_horizon,
)
from utils.horizon import _parse_float


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
    assert _parse_float("not_a_number") == 0.0
    assert _parse_float({}) == 0.0
    assert _parse_float([1, 2, 3]) == 0.0


def test_is_within_horizon_clock_seam_fallback():
    """Verify _is_within_horizon honors patched clock on utils.market_discovery and standalone fallback."""
    import sys

    m = {"ticker": "KXTEST-1", "close_time": "2020-01-04T00:00:00Z"}
    mock_now = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)

    # 1. Honors mocked datetime on utils.market_discovery
    with patch("utils.market_discovery.datetime") as mock_dt:
        mock_dt.datetime.now.return_value = mock_now
        assert _is_within_horizon(m, max_days=7.0) is True
        mock_dt.datetime.now.assert_called_once()

    # 2. Standalone fallback when utils.market_discovery is not in sys.modules
    saved_md = sys.modules.pop("utils.market_discovery", None)
    try:
        assert _is_within_horizon({"ticker": "KXTEST-2"}, max_days=7.0) is True
    finally:
        if saved_md is not None:
            sys.modules["utils.market_discovery"] = saved_md
