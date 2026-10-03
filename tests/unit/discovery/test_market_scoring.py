"""
Unit Tests for Market Scoring and Orderbook Preflight Probing

Tests liquidity scoring:
- V2 float string fields and horizon multiplier
- Two-sided resting orderbook validation (orderbook_fp and legacy)
- Preflight candidate orderbook filtering
- Fail-closed behavior on starved orderbooks
- Defensive fallback and budget exhaustion branches
"""

import datetime
import pytest
import requests
import sys
from unittest.mock import patch, MagicMock

from utils.market_discovery import (
    check_orderbook_has_quotes,
    _liquidity_key,
    _select_best_market,
)
from utils.market_scoring import _get_orderbook_checker


def test_liquidity_key_v2_fields_and_horizon_weighting():
    """Verify _liquidity_key parses v2 float string fields and applies horizon multiplier."""
    now = datetime.datetime.now(datetime.timezone.utc)
    near_term_close = (now + datetime.timedelta(days=3)).isoformat()
    distant_close = (now + datetime.timedelta(days=500)).isoformat()

    # Near term (3 days) contract with v2 float string fields
    near_term_market = {
        "ticker": "KXNFLGAME-26SEP17DETBUF",
        "volume_fp": "25000.50",
        "open_interest_fp": "1200.00",
        "yes_bid_dollars": "0.3200",
        "yes_ask_dollars": "0.3500",
        "close_time": near_term_close,  # near-term
    }

    # Distant futures prop (e.g. 2 years out) with high legacy volume
    distant_market = {
        "ticker": "KXNFLENDSTREAK-40NYJ-2627",
        "volume": 50000,
        "open_interest": 2000,
        "yes_bid": 10,
        "yes_ask": 25,
        "close_time": distant_close,  # distant horizon (>365d)
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


def test_market_scoring_missing_coverage_branches():
    """Verify market scoring fallback checker, horizon brackets, empty candidates, and empty tickers."""
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


def test_liquidity_key_penalizes_blowout_markets():
    """Verify that markets with extreme blowout prices (<= 5c or >= 95c) are heavily penalized in ranking."""
    now = datetime.datetime.now(datetime.timezone.utc)
    close_time = (now + datetime.timedelta(days=2)).isoformat()

    # Blowout game with high volume but priced at 99c
    blowout_market = {
        "ticker": "KXNCAAFGAME-BLOWOUT-ALA",
        "volume_fp": "5000000.00",
        "last_price_dollars": "0.9900",
        "close_time": close_time,
    }

    # Competitive game with lower volume but priced at 55c
    competitive_market = {
        "ticker": "KXNCAAFGAME-COMPETITIVE-FLA",
        "volume_fp": "20000.00",
        "last_price_dollars": "0.5500",
        "close_time": close_time,
    }

    # Competitive market should outscore blowout market despite 250x volume difference
    assert _liquidity_key(competitive_market) > _liquidity_key(blowout_market)

