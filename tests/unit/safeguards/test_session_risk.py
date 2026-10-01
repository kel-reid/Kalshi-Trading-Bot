"""
Unit Test Suite: Session Risk Safeguards & Baseline Telemetry

Validates:
1. Baseline financial telemetry lifecycle: publication of balance, inventory, realized/unrealized PnL, and fees on every tick.
2. Fee churn circuit breaker: cancels quotes and triggers rotation or quiescence liquidation when fee limits are breached.
3. Session stop-loss: cancels quotes, executes exit hedges or inventory liquidation when net loss thresholds are exceeded.
4. Quiescence and shutdown liquidation safeguards, including alerting on failure.
5. Order placement failure backoff and cooldown.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from strategy.market_maker import AvellanedaStoikovBot
from utils.metrics import (
    KALSHI_REALIZED_PNL_CENTS,
    KALSHI_UNREALIZED_PNL_CENTS,
    KALSHI_FEES_PAID_CENTS,
    BOT_PNL_CENTS,
    BOT_INVENTORY_NET_POSITION,
)


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


class TestSessionRiskSafeguards:
    """Verify fee churn circuit breaker and session stop-loss triggers."""

    @pytest.mark.asyncio
    async def test_fee_churn_circuit_breaker_cancels_and_rotates(self, monkeypatch):
        """When accumulated fees meet or exceed max_session_fees_cents, quotes are cancelled and bot rotates."""
        ticker = "KXCHURN-TICKER"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            order_dollars=1.0,
            auto_rotate=True,
            max_session_fees_cents=150,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.rotate_market = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Fees reached 155c (over 150c limit)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "realized_pnl_cents": 10.0,
            "unrealized_pnl_cents": 0.0,
            "total_fees_cents": 155.0,
        })

        monkeypatch.setattr(
            "strategy.market_maker.discover_active_market_async",
            AsyncMock(return_value="KXREPLACEMENT-TICKER")
        )

        await bot._tick()

        bot._cancel_all_quotes.assert_called_once()
        bot.rotate_market.assert_called_once_with("KXREPLACEMENT-TICKER")

    @pytest.mark.asyncio
    async def test_fee_churn_uses_session_fees_not_lifetime_market_fees(self):
        """When lifetime market fees are high but current session fees are low, circuit breaker does not trigger."""
        ticker = "KXCHURN-PREV-LIFETIME"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            order_dollars=1.0,
            auto_rotate=True,
            max_session_fees_cents=150,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.rotate_market = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Lifetime total fees are 300c, but session fees are only 40c (< 150c limit)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "realized_pnl_cents": 50.0,
            "session_realized_pnl_cents": 10.0,
            "unrealized_pnl_cents": 0.0,
            "total_fees_cents": 300.0,
            "session_fees_cents": 40.0,
        })

        await bot._tick()

        # Quoting proceeds normally, circuit breaker does NOT trip
        bot._cancel_all_quotes.assert_not_called()
        bot.rotate_market.assert_not_called()
        bot._update_quotes.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_stop_loss_cancels_and_rotates(self, monkeypatch):
        """When net session PnL breaches max_session_loss_cents, quotes are cancelled and bot rotates."""
        ticker = "KXLOSS-TICKER"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            order_dollars=1.0,
            auto_rotate=True,
            max_session_loss_cents=200,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.rotate_market = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot.inv_manager.get_position = MagicMock(return_value=0)

        # Net loss is -250c (exceeds -200c stop-loss limit)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "realized_pnl_cents": -200.0,
            "unrealized_pnl_cents": -50.0,
            "total_fees_cents": 20.0,
        })

        monkeypatch.setattr(
            "strategy.market_maker.discover_active_market_async",
            AsyncMock(return_value="KXREPLACEMENT-TICKER")
        )

        await bot._tick()

        bot._cancel_all_quotes.assert_called_once()
        bot.rotate_market.assert_called_once_with("KXREPLACEMENT-TICKER")

    @pytest.mark.asyncio
    async def test_session_stop_loss_with_inventory_executes_exit_hedge_only(self, monkeypatch):
        """When net session PnL breaches max_session_loss_cents with high inventory, only the single-sided exit hedge runs; cancellation and rotation are skipped on this tick."""
        ticker = "KXLOSS-HEDGE-TICKER"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            order_dollars=1.0,
            auto_rotate=True,
            max_session_loss_cents=200,
            max_hedge_inventory=10,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)
        bot.rotate_market = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        # Inventory = 15 (high long inventory >= hedge_threshold 10)
        bot.inv_manager.get_position = MagicMock(return_value=15)
        bot.inv_manager.pnl_tracker.get_realized_pnl = MagicMock(return_value=-200.0)
        bot.inv_manager.pnl_tracker.get_unrealized_pnl = MagicMock(return_value=-50.0)

        # Net loss is -250c (exceeds -200c stop-loss limit)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "realized_pnl_cents": -200.0,
            "unrealized_pnl_cents": -50.0,
            "total_fees_cents": 20.0,
        })

        monkeypatch.setattr(
            "strategy.market_maker.discover_active_market_async",
            AsyncMock(return_value="KXREPLACEMENT-TICKER")
        )

        await bot._tick()

        bot._cancel_all_quotes.assert_not_called()
        bot.rotate_market.assert_not_called()
        bot._update_quotes.assert_called_once_with(None, 48)

    @pytest.mark.asyncio
    async def test_order_placement_failure_initiates_cooldown(self):
        """When place_order fails, placement is throttled during the cooldown window."""
        ticker = "KXCOOLDOWN-TICKER"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            order_size=1,
            auto_rotate=False,
        )

        # First attempt: place_order fails (returns None)
        bot.om.place_order = AsyncMock(return_value=None)
        await bot._update_quotes(new_bid=48, new_ask=52)

        assert bot.current_bid_id is None
        assert bot._last_order_error_time > 0
        call_count_1 = bot.om.place_order.call_count

        # Second attempt immediately after: within 5s cooldown, should NOT call place_order again
        await bot._update_quotes(new_bid=48, new_ask=52)
        assert bot.om.place_order.call_count == call_count_1

    @pytest.mark.asyncio
    async def test_session_stop_loss_quiesce_liquidates_inventory_without_autorotate(self):
        """When session stop loss triggers with auto_rotate=False and open inventory, inventory is liquidated."""
        ticker = "KXLOSS-QUIESCE"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            auto_rotate=False,
            max_session_loss_cents=200,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.liquidate_inventory = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        # Open long position of 4 contracts (< hedge_threshold, so it doesn't hedge)
        bot.inv_manager.get_position = MagicMock(return_value=4)

        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "realized_pnl_cents": -210.0,
            "unrealized_pnl_cents": 0.0,
            "total_fees_cents": 10.0,
        })

        await bot._tick()

        bot._cancel_all_quotes.assert_called_once()
        bot.liquidate_inventory.assert_awaited_once_with(ticker)
        assert bot._market_inactive is True

    @pytest.mark.asyncio
    async def test_fee_churn_quiesce_liquidates_inventory_without_autorotate(self):
        """When fee churn triggers with auto_rotate=False and open inventory, inventory is liquidated."""
        ticker = "KXCHURN-QUIESCE"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            auto_rotate=False,
            max_session_fees_cents=100,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.liquidate_inventory = AsyncMock(return_value=True)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        # Open short position of -3 contracts
        bot.inv_manager.get_position = MagicMock(return_value=-3)

        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "session_fees_cents": 150.0,
            "session_realized_pnl_cents": 0.0,
            "unrealized_pnl_cents": 0.0,
        })

        await bot._tick()

        bot._cancel_all_quotes.assert_called_once()
        bot.liquidate_inventory.assert_awaited_once_with(ticker)
        assert bot._market_inactive is True

    @pytest.mark.asyncio
    async def test_price_collar_quiesce_liquidates_inventory_without_autorotate(self):
        """When price collar is breached with auto_rotate=False and open inventory, inventory is liquidated."""
        ticker = "KXCOLLAR-QUIESCE"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            min_mid_price=10,
            max_mid_price=90,
            auto_rotate=False,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.liquidate_inventory = AsyncMock(return_value=True)
        # Mid price 5c (below 10c collar)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(4, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(6, 10))
        bot.inv_manager.get_position = MagicMock(return_value=2)

        await bot._tick()

        bot._cancel_all_quotes.assert_called_once()
        bot.liquidate_inventory.assert_awaited_once_with(ticker)
        assert bot._market_inactive is True

    @pytest.mark.asyncio
    async def test_bot_stop_liquidates_open_inventory(self):
        """When bot.stop() is called with open inventory, it executes liquidation."""
        ticker = "KXSTOP-LIQ"
        bot = AvellanedaStoikovBot(ticker=ticker)
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.liquidate_inventory = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=7)

        stopped = await bot.stop()

        assert stopped is True
        bot.liquidate_inventory.assert_awaited_once_with(ticker)

    @pytest.mark.asyncio
    async def test_quiesce_liquidation_failure_sends_alert(self):
        """When safeguard quiescence liquidation fails, send_alert is triggered to notify of unhedged exposure."""
        ticker = "KXFAIL-LIQ-ALERT"
        bot = AvellanedaStoikovBot(
            ticker=ticker,
            gamma=0.5,
            min_spread=4,
            auto_rotate=False,
            max_session_loss_cents=200,
        )
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot.liquidate_inventory = AsyncMock(return_value=False)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot.inv_manager.get_position = MagicMock(return_value=3)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "realized_pnl_cents": -250.0,
            "unrealized_pnl_cents": 0.0,
            "total_fees_cents": 10.0,
        })

        with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
            await bot._tick()

            bot.liquidate_inventory.assert_awaited_once_with(ticker)
            assert bot._market_inactive is True
            mock_alert.assert_awaited_once()
            assert f"Safeguard liquidation failed after session_stop_loss on {ticker}" in mock_alert.await_args[0][0]
