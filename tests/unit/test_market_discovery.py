"""
Unit tests for Market Discovery, Dynamic Rotation, and Starvation Alerting.
"""

import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock

from utils.market_discovery import (
    discover_active_market,
    check_market_status,
    is_market_active,
    SPORTS_KEYWORDS,
    FALLBACK_KEYWORDS
)
from strategy.market_maker import AvellanedaStoikovBot


MOCK_MARKETS_RAW = [
    {"ticker": "KXNFL-26SEP14-KC", "status": "active"},
    {"ticker": "KXMLB-26SEP14-NYY", "status": "active"},
    {"ticker": "KXINX-26SEP14-5800", "status": "active"},
    {"ticker": "KXMVE-COMBO-123", "status": "active"}, # Should be filtered out
    {"ticker": "KXCLOSED-TEST", "status": "closed"},   # Should be filtered out
]


def test_discover_exact_match():
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXNFL-26SEP14-KC", "status": "active"},
            {"ticker": "KXINX-26SEP14-5800", "status": "active"},
        ]
        result = discover_active_market(target_preference="KXINX-26SEP14-5800")
        assert result == "KXINX-26SEP14-5800"


def test_discover_sports_keyword():
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXNFL-26SEP14-KC", "status": "active"},
            {"ticker": "KXINX-26SEP14-5800", "status": "active"},
        ]
        result = discover_active_market(target_preference="NFL")
        assert result == "KXNFL-26SEP14-KC"


def test_discover_category_sports():
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXMLB-26SEP14-NYY", "status": "active"},
            {"ticker": "KXINX-26SEP14-5800", "status": "active"},
        ]
        result = discover_active_market(target_preference="SPORTS")
        assert result == "KXMLB-26SEP14-NYY"


def test_discover_fallback_when_sports_unavailable():
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXINX-26SEP14-5800", "status": "active"},
        ]
        result = discover_active_market(target_preference="NFL")
        assert result == "KXINX-26SEP14-5800"


def test_discover_exclude_tickers():
    with patch("utils.market_discovery.fetch_eligible_markets") as mock_fetch:
        mock_fetch.return_value = [
            {"ticker": "KXNFL-FIRST", "status": "active"},
            {"ticker": "KXNFL-SECOND", "status": "active"},
        ]
        result = discover_active_market(
            target_preference="NFL",
            exclude_tickers=["KXNFL-FIRST"]
        )
        assert result == "KXNFL-SECOND"


def test_check_market_status_active():
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"market": {"ticker": "TEST-1", "status": "active"}}
        mock_get.return_value = mock_resp

        status = check_market_status("TEST-1")
        assert status == "active"
        assert is_market_active("TEST-1") is True


def test_check_market_status_closed():
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"market": {"ticker": "TEST-1", "status": "settled"}}
        mock_get.return_value = mock_resp

        status = check_market_status("TEST-1")
        assert status == "settled"
        assert is_market_active("TEST-1") is False


@pytest.mark.asyncio
async def test_bot_rotate_market():
    """Verify that rotate_market cancels quotes, resubscribes, and sends alert."""
    bot = AvellanedaStoikovBot(ticker="OLD-TICKER", gamma=0.5, min_spread=4, order_size=1)
    bot._cancel_all_quotes = AsyncMock()
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        await bot.rotate_market("NEW-TICKER")

        bot._cancel_all_quotes.assert_awaited_once()
        bot.ob_manager.unsubscribe.assert_awaited_once_with(["OLD-TICKER"])
        bot.ob_manager.subscribe.assert_awaited_once_with(["NEW-TICKER"])
        assert bot.ticker == "NEW-TICKER"
        mock_alert.assert_awaited_once()
        assert "NEW-TICKER" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_bot_starvation_alerting():
    """Verify that orderbook starvation triggers an alert after timeout and resets on recovery."""
    bot = AvellanedaStoikovBot(
        ticker="MOCK_TICKER",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        starvation_timeout=900.0,
        auto_rotate=False
    )
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot._cancel_all_quotes = AsyncMock()
    bot._update_quotes = AsyncMock()

    # 1. Orderbook empty: first tick sets starvation start time
    bot.ob_manager.get_best_bid = MagicMock(return_value=None)
    bot.ob_manager.get_best_ask = MagicMock(return_value=None)

    with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
        await bot._tick()
        assert bot._starvation_start_time is not None
        assert bot._starvation_alert_sent is False
        mock_alert.assert_not_called()

        # 2. Simulate 901 seconds elapsed (past 15 minute threshold)
        bot._starvation_start_time = time.time() - 901
        await bot._tick()
        assert bot._starvation_alert_sent is True
        mock_alert.assert_awaited_once()
        assert "STARVATION ALERT" in mock_alert.call_args[0][0]

        # 3. Next tick while still starved: do NOT spam duplicate alert
        mock_alert.reset_mock()
        await bot._tick()
        mock_alert.assert_not_called()

        # 4. Liquidity returns: alert restoration and reset timers
        bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(54, 10))
        await bot._tick()
        assert bot._starvation_alert_sent is False
        assert bot._starvation_start_time is None
        mock_alert.assert_awaited_once()
        assert "Orderbook Restored" in mock_alert.call_args[0][0]
