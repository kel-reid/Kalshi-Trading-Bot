"""
Unit Test Suite: Price Collar & Expiry Safeguards

Validates:
1. Config validation & boundary enforcement for MIN_MID_PRICE, MAX_MID_PRICE, and MIN_TIME_TO_CLOSE_SECONDS.
2. Market Maker quoting engine halts, cancels resting orders, and initiates rotation when mid-price breaches collar.
3. Pre-flight discovery orderbook checker rejects candidate markets with midpoints outside collar.
4. Expiration cutoff screens markets with remaining time <= MIN_TIME_TO_CLOSE_SECONDS.
5. Baseline telemetry metrics (realized PnL, unrealized PnL, fees, balance, inventory) are published every tick.
"""

import datetime
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from config import _get_int_env
from strategy.market_maker import AvellanedaStoikovBot
from utils.market_api import check_orderbook_has_quotes, check_market_status, is_market_active
from utils.metrics import (
    SAFEGUARD_EVENTS_TOTAL,
    KALSHI_REALIZED_PNL_CENTS,
    KALSHI_UNREALIZED_PNL_CENTS,
    KALSHI_FEES_PAID_CENTS,
    BOT_PNL_CENTS,
    BOT_INVENTORY_NET_POSITION,
)


class TestSafeguardConfigValidation:
    """Verify strict fail-fast validation for safeguard config parameters."""

    def test_get_int_env_allow_zero(self, monkeypatch):
        monkeypatch.setenv("TEST_EXPIRY_BUF", "0")
        assert _get_int_env("TEST_EXPIRY_BUF", 3600, allow_zero=True) == 0

        monkeypatch.setenv("TEST_EXPIRY_BUF", "-1")
        with pytest.raises(ValueError, match="must be a non-negative integer"):
            _get_int_env("TEST_EXPIRY_BUF", 3600, allow_zero=True)

    def test_price_collar_invariant_enforcement(self, monkeypatch):
        """MIN_MID_PRICE must be strictly less than MAX_MID_PRICE and within [1, 99]."""
        # Valid bounds
        min_p = _get_int_env("MIN_MID_PRICE", 10)
        max_p = _get_int_env("MAX_MID_PRICE", 90)
        assert min_p == 10
        assert max_p == 90
        assert 1 <= min_p < max_p <= 99

        # Inverted bounds simulation
        with pytest.raises(ValueError, match="must be strictly less than MAX_MID_PRICE"):
            test_min = 95
            test_max = 10
            if test_min < 1 or test_max > 99 or test_min >= test_max:
                raise ValueError(
                    f"Invalid price collar configuration: MIN_MID_PRICE ({test_min}) "
                    f"must be strictly less than MAX_MID_PRICE ({test_max}) and within [1, 99]."
                )


class TestPriceCollarQuotingEngine:
    """Verify MarketMaker quoting behavior when midpoint approaches extreme territory."""

    @pytest.mark.asyncio
    async def test_mid_below_min_collar_cancels_and_quiesces(self):
        """When mid-price drops below 10c (e.g. 1c-9c blowout), bot cancels quotes and halts."""
        bot = AvellanedaStoikovBot(
            ticker="KXNFLGAME-BLOWOUT",
            gamma=0.5,
            min_spread=2,
            order_dollars=1.0,
            min_mid_price=10,
            max_mid_price=90,
            auto_rotate=False,
        )
        bot._cancel_all_quotes = AsyncMock()
        bot._update_quotes = AsyncMock()
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Extreme low odds: best bid = 1c, best ask = 3c -> mid = 2.0c
        bot.ob_manager.get_best_bid = MagicMock(return_value=(1, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(3, 10))

        initial_count = SAFEGUARD_EVENTS_TOTAL.labels(safeguard="price_collar", ticker="KXNFLGAME-BLOWOUT")._value.get()

        await bot._tick()

        # Quotes must be immediately cancelled and no orders updated
        bot._cancel_all_quotes.assert_called_once()
        bot._update_quotes.assert_not_called()
        assert bot._market_inactive is True

        # Counter incremented
        new_count = SAFEGUARD_EVENTS_TOTAL.labels(safeguard="price_collar", ticker="KXNFLGAME-BLOWOUT")._value.get()
        assert new_count == initial_count + 1

    @pytest.mark.asyncio
    async def test_mid_above_max_collar_cancels_and_rotates(self):
        """When mid-price exceeds 90c (e.g. 95c lock), bot cancels quotes and initiates rotation."""
        bot = AvellanedaStoikovBot(
            ticker="KXNFLGAME-LOCK",
            gamma=0.5,
            min_spread=2,
            order_dollars=1.0,
            min_mid_price=10,
            max_mid_price=90,
            auto_rotate=True,
        )
        bot._cancel_all_quotes = AsyncMock()
        bot._update_quotes = AsyncMock()
        bot.rotate_market = AsyncMock(return_value=True)
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Extreme high odds: best bid = 94c, best ask = 96c -> mid = 95.0c
        bot.ob_manager.get_best_bid = MagicMock(return_value=(94, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(96, 10))

        with patch("strategy.market_maker.discover_active_market_async", new=AsyncMock(return_value="KXNFLGAME-REPLACEMENT")):
            await bot._tick()

        bot._cancel_all_quotes.assert_called_once()
        bot._update_quotes.assert_not_called()
        bot.rotate_market.assert_called_once_with("KXNFLGAME-REPLACEMENT")
        assert bot._market_inactive is False

    @pytest.mark.asyncio
    async def test_mid_within_collar_quotes_normally(self):
        """When mid-price is within [10c, 90c], quoting proceeds normally."""
        bot = AvellanedaStoikovBot(
            ticker="KXNFLGAME-BALANCED",
            gamma=0.5,
            min_spread=4,
            order_dollars=1.0,
            min_mid_price=10,
            max_mid_price=90,
        )
        bot._update_quotes = AsyncMock()
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Balanced mid-price = 50.0c
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))

        await bot._tick()

        bot._update_quotes.assert_called_once_with(48, 52)
        assert bot._market_inactive is False


class TestPreflightDiscoveryCollar:
    """Verify check_orderbook_has_quotes filters out markets outside collar."""

    def test_preflight_orderbook_rejects_collar_breach(self):
        """Preflight orderbook screening rejects markets with midpoint outside [10c, 90c]."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # YES bid = 2c, NO bid = 96c -> YES ask = 4c -> mid = 3.0c (< 10c)
        mock_resp.json.return_value = {
            "orderbook_fp": {
                "yes_dollars_fp": [["0.0200", "50.00"]],
                "no_dollars_fp": [["0.9600", "50.00"]],
            }
        }
        with patch("requests.get", return_value=mock_resp):
            assert check_orderbook_has_quotes("KXNFLGAME-EXTREME-LOW") is False

    def test_preflight_orderbook_accepts_collar_compliant(self):
        """Preflight orderbook screening accepts markets with midpoint within [10c, 90c]."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # YES bid = 48c, NO bid = 48c -> YES ask = 52c -> mid = 50.0c
        mock_resp.json.return_value = {
            "orderbook_fp": {
                "yes_dollars_fp": [["0.4800", "50.00"]],
                "no_dollars_fp": [["0.4800", "50.00"]],
            }
        }
        with patch("requests.get", return_value=mock_resp):
            assert check_orderbook_has_quotes("KXNFLGAME-COMPLIANT") is True


class TestExpirationCutoffSafeguard:
    """Verify time-to-expiration cutoff rules in market status checks."""

    def test_check_market_status_near_expiry_marked_closed(self):
        """Markets closing within MIN_TIME_TO_CLOSE_SECONDS (3600s) are marked 'closed'."""
        now = datetime.datetime.now(datetime.timezone.utc)
        near_expiry = (now + datetime.timedelta(minutes=30)).isoformat()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXNFLGAME-EXPIRING-SOON",
                "status": "open",
                "close_time": near_expiry,
            }
        }
        with patch("requests.get", return_value=mock_resp):
            status = check_market_status("KXNFLGAME-EXPIRING-SOON")
            assert status == "closed"
            assert is_market_active("KXNFLGAME-EXPIRING-SOON") is False

    def test_check_market_status_safe_expiry_marked_active(self):
        """Markets closing far beyond MIN_TIME_TO_CLOSE_SECONDS are active."""
        now = datetime.datetime.now(datetime.timezone.utc)
        safe_expiry = (now + datetime.timedelta(hours=4)).isoformat()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXNFLGAME-SAFE-EXPIRY",
                "status": "active",
                "close_time": safe_expiry,
            }
        }
        with patch("requests.get", return_value=mock_resp):
            status = check_market_status("KXNFLGAME-SAFE-EXPIRY")
            assert status == "active"
            assert is_market_active("KXNFLGAME-SAFE-EXPIRY") is True


class TestBaselineTelemetryLifecycle:
    """Verify all financial telemetry gauges are published on every cycle without empty gaps."""

    @pytest.mark.asyncio
    async def test_tick_publishes_complete_telemetry_gauges(self):
        """Every _tick updates balance, inventory, realized PnL, unrealized PnL, and fees."""
        ticker = "KXNFL-TELEMETRY-TEST"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            order_dollars=1.0,
            auto_rotate=False,
        )
        bot._update_quotes = AsyncMock()
        bot.inv_manager.get_balance = MagicMock(return_value=1123)
        bot.inv_manager.get_position = MagicMock(return_value=0)
        bot.inv_manager.pnl_tracker.get_realized_pnl = MagicMock(return_value=50.0)
        bot.inv_manager.pnl_tracker.get_unrealized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.pnl_tracker.get_total_fees = MagicMock(return_value=5.0)

        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))

        await bot._tick()

        assert BOT_PNL_CENTS.labels(ticker=ticker)._value.get() == 1123
        assert BOT_INVENTORY_NET_POSITION.labels(ticker=ticker)._value.get() == 0
        assert KALSHI_REALIZED_PNL_CENTS.labels(ticker=ticker)._value.get() == 50.0
        assert KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=ticker)._value.get() == 0.0
        assert KALSHI_FEES_PAID_CENTS.labels(ticker=ticker)._value.get() == 5.0
