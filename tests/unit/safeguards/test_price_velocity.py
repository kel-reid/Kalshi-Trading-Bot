"""
Unit Test Suite: Fast Market / Price Velocity Circuit Breaker Safeguards

Validates:
1. Rapid price swings exceeding threshold within rolling window trigger Fast Market state.
2. Resting quotes are immediately withdrawn and quote placement is suppressed upon trigger.
3. Fast market quiesce period suppresses quote placement while active.
4. Quoting resumes normally once quiesce window expires.
5. Slow, gradual price drift within normal market noise does not trigger circuit breaker.
6. Setting price_velocity_threshold_cents to 0.0 cleanly disables the circuit breaker.
7. Market rotation clears historical velocity samples and resets fast market state.
"""

import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from strategy.market_maker import AvellanedaStoikovBot


class TestPriceVelocityCircuitBreaker:
    """Verify Fast Market / Price Velocity Circuit Breaker mechanics and lifecycle."""

    @pytest.mark.asyncio
    async def test_price_velocity_breaker_triggers_on_rapid_price_swing(self):
        """When midpoint swings by >= threshold within window, quotes are withdrawn and quiesce activated."""
        ticker = "KXVELOCITY-TRIGGER"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            price_velocity_threshold_cents=6.0,
            price_velocity_window_seconds=20.0,
            price_velocity_quiesce_seconds=30.0,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # First tick at t0: mid = 50.0c (bid 48, ask 52)
        now = time.time()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        await bot._tick()
        assert bot._update_quotes.call_count == 1
        assert bot._fast_market_until == 0.0

        # Second tick at t0 + 5.0s: mid jumps to 57.0c (bid 55, ask 59) -> delta 7.0c >= 6.0c
        bot._update_quotes.reset_mock()
        bot._cancel_all_quotes.reset_mock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(55, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(59, 10))

        with patch("time.time", return_value=now + 5.0):
            await bot._tick()

        # Quotes must be cancelled, new placement suppressed, and quiesce active for 30s
        bot._cancel_all_quotes.assert_called_once()
        bot._update_quotes.assert_not_called()
        assert bot._fast_market_until == pytest.approx(now + 5.0 + 30.0)

    @pytest.mark.asyncio
    async def test_price_velocity_breaker_quiesces_during_active_fast_market(self):
        """While in Fast Market quiesce state, placement is suppressed and active quotes are withdrawn."""
        ticker = "KXVELOCITY-QUIESCE"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            price_velocity_quiesce_seconds=30.0,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=0)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))

        now = time.time()
        bot._fast_market_until = now + 20.0  # 20 seconds remaining
        bot.current_bid_id = "bid-active-1"

        await bot._tick()

        bot._cancel_all_quotes.assert_called_once()
        bot._update_quotes.assert_not_called()

    @pytest.mark.asyncio
    async def test_price_velocity_breaker_resumes_after_quiesce_expires(self):
        """Once fast market quiesce period expires, normal quoting resumes at the new mid price."""
        ticker = "KXVELOCITY-RESUME"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            price_velocity_quiesce_seconds=30.0,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=0)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))

        now = time.time()
        bot._fast_market_until = now - 1.0  # Expired 1 second ago

        await bot._tick()

        # Quoting resumed normally
        bot._update_quotes.assert_called_once_with(48, 52)

    @pytest.mark.asyncio
    async def test_price_velocity_slow_drift_does_not_trigger(self):
        """Gradual price drift within the rolling window does not trigger the circuit breaker."""
        ticker = "KXVELOCITY-SLOW"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            price_velocity_threshold_cents=6.0,
            price_velocity_window_seconds=20.0,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=0)

        now = time.time()
        # Tick 1: mid = 50.0c at t = 0
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        with patch("time.time", return_value=now):
            await bot._tick()
        assert bot._update_quotes.call_count == 1

        # Tick 2: mid = 52.0c at t = 8s (drift 2.0c < 6.0c)
        bot._update_quotes.reset_mock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(50, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(54, 10))
        with patch("time.time", return_value=now + 8.0):
            await bot._tick()
        assert bot._update_quotes.call_count == 1
        assert bot._fast_market_until == 0.0

        # Tick 3: mid = 54.0c at t = 16s (drift 4.0c < 6.0c)
        bot._update_quotes.reset_mock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(52, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(56, 10))
        with patch("time.time", return_value=now + 16.0):
            await bot._tick()
        assert bot._update_quotes.call_count == 1
        assert bot._fast_market_until == 0.0

    @pytest.mark.asyncio
    async def test_price_velocity_disabled_when_threshold_zero(self):
        """Setting price_velocity_threshold_cents to 0.0 disables the circuit breaker."""
        ticker = "KXVELOCITY-DISABLED"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            price_velocity_threshold_cents=0.0,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=0)

        now = time.time()
        # Tick 1: mid = 50.0c
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        with patch("time.time", return_value=now):
            await bot._tick()

        # Tick 2: mid = 80.0c (massive 30c jump, but threshold is 0.0 -> disabled)
        bot._update_quotes.reset_mock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(78, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(82, 10))
        with patch("time.time", return_value=now + 2.0):
            await bot._tick()

        assert bot._update_quotes.call_count == 1
        assert bot._fast_market_until == 0.0

    @pytest.mark.asyncio
    async def test_price_velocity_reset_on_market_rotation(self):
        """Market rotation clears velocity samples and resets fast market quiesce state."""
        bot = AvellanedaStoikovBot(
            ticker="KXOLD-TICKER",
            gamma=0.5,
            min_spread=4,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.om.reconcile_resting_orders = AsyncMock(return_value={"cancelled": [], "orphans": []})
        bot.liquidate_inventory = AsyncMock(return_value=True)
        bot.ob_manager.unsubscribe = AsyncMock(return_value=True)
        bot.ob_manager.subscribe = AsyncMock(return_value=True)
        bot.om.record_pnl_snapshot_async = AsyncMock(return_value=True)

        # Populate state
        bot._mid_price_history.append((time.time(), 50.0))
        bot._mid_price_history.append((time.time() + 1.0, 58.0))
        bot._fast_market_until = time.time() + 30.0

        success = await bot.rotate_market("KXNEW-TICKER")
        assert success is True
        assert len(bot._mid_price_history) == 0
        assert bot._fast_market_until == 0.0
