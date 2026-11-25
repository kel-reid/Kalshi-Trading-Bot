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
from decimal import Decimal
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
        bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})

        # Populate state
        bot._mid_price_history.append((time.time(), 50.0))
        bot._mid_price_history.append((time.time() + 1.0, 58.0))
        bot._fast_market_until = time.time() + 30.0

        success = await bot.rotate_market("KXNEW-TICKER")
        assert success is True
        assert len(bot._mid_price_history) == 0
        assert bot._fast_market_until == 0.0

    @pytest.mark.asyncio
    async def test_fast_market_trigger_long_inventory_keeps_exit_ask_active(self):
        """When Fast Market triggers while long, suppress bids only and keep exit ask active."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=1,
            price_velocity_threshold_cents=5.0,
            price_velocity_window_seconds=10.0,
            price_velocity_quiesce_seconds=30.0,
            post_fill_pause_seconds=0.0,
        )
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=3)
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)

        now = 1_000_000.0
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        with patch("time.time", return_value=now):
            await bot._tick()

        # Tick 2: +6c move triggers Fast Market while carrying +3 contracts
        bot._update_quotes.reset_mock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(54, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(58, 10))
        with patch("time.time", return_value=now + 2.0):
            await bot._tick()

        assert bot._fast_market_until == now + 2.0 + 30.0
        bot._cancel_all_quotes.assert_not_called()
        bot._update_quotes.assert_called_once()
        bid, ask = bot._update_quotes.call_args[0]
        assert bid is None
        assert ask is not None
        assert bot._pause_accumulating_side is True

    @pytest.mark.asyncio
    async def test_fast_market_active_short_inventory_keeps_exit_bid_active(self):
        """While in active Fast Market cooldown with short inventory, keep covering bid active only."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=1,
            post_fill_pause_seconds=0.0,
        )
        now = 1_000_000.0
        bot._fast_market_until = now + 20.0
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=-3)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot._cancel_all_quotes = AsyncMock(return_value=True)
        bot._update_quotes = AsyncMock(return_value=True)

        with patch("time.time", return_value=now + 5.0):
            await bot._tick()

        bot._cancel_all_quotes.assert_not_called()
        bot._update_quotes.assert_called_once()
        bid, ask = bot._update_quotes.call_args[0]
        assert bid is not None
        assert ask is None

    @pytest.mark.asyncio
    async def test_pause_accumulating_side_update_quotes_skips_exit_if_flattened_during_cancel(self):
        """
        With _pause_accumulating_side=True on an exit-only ask quote, if cancelling the
        accumulating bid reveals inventory flattened to 0, no new exit ask is placed.
        """
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=1,
        )
        bot._pause_accumulating_side = True
        bot.current_bid_id = "bid-old"
        bot.current_bid_price = 45
        bot.current_bid_size = 1
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.om.place_order = AsyncMock(return_value="ask-new")
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        # Position flattened to 0 by the time exit ask placement checks post-cancel inventory
        bot.inv_manager.get_position = MagicMock(return_value=0)
        bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})

        await bot._update_quotes(None, 55)

        bot.om.cancel_order.assert_awaited_once_with("bid-old")
        bot.om.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_exit_keeps_valid_sized_resting_ask_without_cancel_replace(self):
        """
        In pause-only mode, if an existing exit ASK already has size <= open inventory,
        _update_quotes keeps it resting untouched rather than introducing a cancel-replace race.
        """
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=5,
        )
        bot._pause_accumulating_side = True
        bot.current_ask_id = "ask-old"
        bot.current_ask_price = 56
        bot.current_ask_size = 3
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.om.place_order = AsyncMock(return_value="ask-new")
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=3)

        await bot._update_quotes(None, 54)

        bot.om.cancel_order.assert_not_called()
        bot.om.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_exit_stale_rest_defers_replacement_when_cancelled_ask_unsettled(self):
        """
        Option A ASK guard: if an old ASK (size 3) was cancelled and 2 of 3 filled silently
        while BOTH WS (_pending_fills=0, local=3) AND REST get-positions are stale (3),
        unsettled cancelled-ask exposure (3) prevents placing a 3-contract replacement ASK
        that would reverse +1 into -2.
        """
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=5,
        )
        bot._pause_accumulating_side = True
        # Case A: pre-cancelled 3-lot ASK before entering pause
        bot._record_unsettled_cancel("ask", "ask-old", 3, kalshi_order_id="k-ask-old")
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.om.place_order = AsyncMock(return_value="ask-new")
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=3)  # Stale WS
        bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("3")})  # Stale REST

        await bot._update_quotes(None, 54)

        bot.om.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_exit_stale_rest_defers_replacement_when_cancelled_bid_unsettled(self):
        """
        Option A BID guard: symmetric short path when old covering BID (size 3) was
        cancelled and BOTH WS (local=-3) AND REST get-positions return stale -3; replacement
        BID is deferred rather than risking a short-to-long reversal.
        """
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=5,
        )
        bot._pause_accumulating_side = True
        bot._record_unsettled_cancel("bid", "bid-old", 3, kalshi_order_id="k-bid-old")
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.om.place_order = AsyncMock(return_value="bid-new")
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=-3)  # Stale WS
        bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("-3")})  # Stale REST

        await bot._update_quotes(46, None)

        bot.om.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_exit_stale_rest_caps_ask_and_bid_to_surplus_above_unsettled_cancel(self):
        """
        With +5 local / stale +5 REST and a cancelled 2-lot ASK unsettled, pause exit
        safely places at most 5 - 2 = 3 contracts (same symmetrically on BID for -5 / -2).
        """
        bot_long = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=5,
        )
        bot_long._pause_accumulating_side = True
        bot_long._record_unsettled_cancel("ask", "ask-2", 2, kalshi_order_id="k-ask-2")
        bot_long.om.place_order = AsyncMock(return_value="ask-safe")
        bot_long.inv_manager.get_balance = MagicMock(return_value=10000)
        bot_long.inv_manager.get_position = MagicMock(return_value=5)
        bot_long.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("5")})

        await bot_long._update_quotes(None, 54)
        bot_long.om.place_order.assert_awaited_once()
        assert bot_long.om.place_order.call_args.kwargs["action"] == "sell"
        assert bot_long.om.place_order.call_args.kwargs["count"] == 3

        bot_short = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=5,
        )
        bot_short._pause_accumulating_side = True
        bot_short._record_unsettled_cancel("bid", "bid-2", 2, kalshi_order_id="k-bid-2")
        bot_short.om.place_order = AsyncMock(return_value="bid-safe")
        bot_short.inv_manager.get_balance = MagicMock(return_value=10000)
        bot_short.inv_manager.get_position = MagicMock(return_value=-5)
        bot_short.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("-5")})

        await bot_short._update_quotes(46, None)
        bot_short.om.place_order.assert_awaited_once()
        assert bot_short.om.place_order.call_args.kwargs["action"] == "buy"
        assert bot_short.om.place_order.call_args.kwargs["count"] == 3

    @pytest.mark.asyncio
    async def test_pause_exit_oversized_resting_ask_cancelled_and_deferred_under_stale_rest(self):
        """
        If an oversized 5-lot ASK is cancelled on entering pause (inv=3) and REST still
        reports stale +3, the 5-lot unsettled cancel keeps replacement deferred until settled.
        """
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=5,
        )
        bot._pause_accumulating_side = True
        bot.current_ask_id = "ask-oversized"
        bot.current_ask_price = 56
        bot.current_ask_size = 5
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.om.place_order = AsyncMock(return_value="ask-new")
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=3)
        bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("3")})

        await bot._update_quotes(None, 54)

        bot.om.cancel_order.assert_awaited_once_with("ask-oversized")
        bot.om.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_exit_after_settle_reserves_remote_fills_and_releases_zero_fill_cancel(self, monkeypatch):
        """
        After _pause_exit_settle_seconds elapses:
        - If get_order_status reports fill_count=2 with unchanged _fill_count, only 1 (3-2) is allowed even under stale REST=3.
        - If get_order_status confirms status='canceled', fill_count=0, reservation releases and full 3 places.
        """
        mono = [100.0]
        monkeypatch.setattr("strategy.market_maker.time.monotonic", lambda: mono[0])

        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=5,
        )
        bot._pause_accumulating_side = True
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=3)
        bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("3")})

        # 1. Cancelled 3-lot order had 2 remote fills that local WS hasn't delivered yet -> only 1 allowed
        bot._record_unsettled_cancel("ask", "ask-part", 3, kalshi_order_id="k-part")
        bot.om.get_order_status = AsyncMock(return_value={"status": "canceled", "remaining_count": 0, "fill_count": 2})
        bot.om.place_order = AsyncMock(return_value="ask-1")
        mono[0] += bot._pause_exit_settle_seconds + 0.5

        await bot._update_quotes(None, 54)
        bot.om.place_order.assert_awaited_once()
        assert bot.om.place_order.call_args.kwargs["count"] == 1

        # 2. Reset with clean 0-fill terminal cancel after settle window -> releases full 3
        bot._unsettled_cancelled_asks.clear()
        bot.current_ask_id = None
        bot.current_ask_price = None
        bot.current_ask_size = None
        bot.om.place_order.reset_mock()
        bot._record_unsettled_cancel("ask", "ask-clean", 3, kalshi_order_id="k-clean")
        bot.om.get_order_status = AsyncMock(return_value={"status": "canceled", "remaining_count": 0, "fill_count": 0})
        mono[0] += bot._pause_exit_settle_seconds + 0.5

        await bot._update_quotes(None, 54)
        bot.om.place_order.assert_awaited_once()
        assert bot.om.place_order.call_args.kwargs["count"] == 3

    @pytest.mark.asyncio
    async def test_pause_exit_fails_closed_when_exchange_position_unverified(self):
        """When live exchange position cannot be verified, pause-exit placement defers rather than risking reversal."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TICKER",
            gamma=0.5,
            min_spread=4,
            order_size=3,
        )
        bot._pause_accumulating_side = True
        bot.om.place_order = AsyncMock(return_value="ask-new")
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_position = MagicMock(return_value=3)
        bot.inv_manager._fetch_ticker_positions = MagicMock(return_value=None)

        await bot._update_quotes(None, 54)

        bot.om.place_order.assert_not_called()
