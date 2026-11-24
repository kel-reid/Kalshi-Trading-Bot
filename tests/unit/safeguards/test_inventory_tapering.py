"""
Unit tests for Late-Session Dynamic Inventory Tapering Safeguards.
Verifies pre-blackout sizing scaling, gamma skew boosting, reduce-only quote gating,
and invariant preservation leading into market expiration.
"""

import asyncio
import datetime
from decimal import Decimal
import math
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from strategy.market_maker import AvellanedaStoikovBot
from utils.metrics import SAFEGUARD_EVENTS_TOTAL


class TestInventoryTapering:
    """Tests for get_inventory_taper_factor and pre-blackout quoting behavior."""

    def test_taper_factor_disabled_or_missing_metadata(self):
        """When disabled, unconfigured, or missing close_time, taper factor is always 1.0."""
        # Missing close time
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TAPER",
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        assert bot.get_inventory_taper_factor() == 1.0

        # Tapering explicitly disabled
        now = datetime.datetime.now(datetime.timezone.utc)
        bot._market_close_time = now + datetime.timedelta(seconds=2000)
        bot.inventory_taper_enabled = False
        assert bot.get_inventory_taper_factor(now) == 1.0

        # Window is 0
        bot.inventory_taper_enabled = True
        bot.inventory_taper_window_seconds = 0
        assert bot.get_inventory_taper_factor(now) == 1.0

    def test_taper_factor_outside_taper_window(self):
        """When time to close is beyond blackout + taper_window, taper factor is 1.0."""
        now = datetime.datetime.now(datetime.timezone.utc)
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TAPER",
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        # Blackout at 1800s, taper start at 3600s. Market closes in 4000s.
        bot._market_close_time = now + datetime.timedelta(seconds=4000)
        assert bot.get_inventory_taper_factor(now) == 1.0

    def test_taper_factor_inside_blackout(self):
        """When time to close is at or below blackout threshold, taper factor is 0.0."""
        now = datetime.datetime.now(datetime.timezone.utc)
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TAPER",
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        bot._market_close_time = now + datetime.timedelta(seconds=1800)
        assert bot.get_inventory_taper_factor(now) == 0.0

        bot._market_close_time = now + datetime.timedelta(seconds=900)
        assert bot.get_inventory_taper_factor(now) == 0.0

    def test_taper_factor_linear_scaling(self):
        """Within the taper window, factor scales linearly between 0.0 and 1.0."""
        now = datetime.datetime.now(datetime.timezone.utc)
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TAPER",
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        # Taper window: remaining in [1800, 3600].
        # 1. Exactly halfway (2700s remaining -> 900s into taper): alpha = 900 / 1800 = 0.50
        bot._market_close_time = now + datetime.timedelta(seconds=2700)
        assert pytest.approx(bot.get_inventory_taper_factor(now), abs=1e-4) == 0.50

        # 2. 75% remaining (3150s remaining -> 1350s into taper): alpha = 1350 / 1800 = 0.75
        bot._market_close_time = now + datetime.timedelta(seconds=3150)
        assert pytest.approx(bot.get_inventory_taper_factor(now), abs=1e-4) == 0.75

        # 3. 25% remaining (2250s remaining -> 450s into taper): alpha = 450 / 1800 = 0.25
        bot._market_close_time = now + datetime.timedelta(seconds=2250)
        assert pytest.approx(bot.get_inventory_taper_factor(now), abs=1e-4) == 0.25

    def test_calculate_order_size_with_tapering(self):
        """calculate_order_size scales contract count smoothly down with taper factor."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TAPER",
            order_dollars=5.0,  # At 50c mid -> raw count = 10 contracts
            max_order_contracts=50,
            max_low_price_contracts=10,
        )

        # Baseline: taper_factor=1.0 (default)
        assert bot.calculate_order_size(50.0, taper_factor=1.0) == 10
        assert bot.calculate_order_size(50.0) == 10

        # 50% taper: 10 * 0.5 = 5 contracts
        assert bot.calculate_order_size(50.0, taper_factor=0.5) == 5

        # 25% taper: 10 * 0.25 = 2 contracts
        assert bot.calculate_order_size(50.0, taper_factor=0.25) == 2

        # 5% taper: 10 * 0.05 = floor(0.5) -> clamped to min 1 contract
        assert bot.calculate_order_size(50.0, taper_factor=0.05) == 1
        assert bot.calculate_order_size(50.0, taper_factor=0.0) == 1

        # Non-finite or invalid taper_factor safely falls back to 1.0
        assert bot.calculate_order_size(50.0, taper_factor=float("nan")) == 10
        assert bot.calculate_order_size(50.0, taper_factor=-1.0) == 10
        assert bot.calculate_order_size(50.0, taper_factor=True) == 10

    def test_calculate_order_size_low_price_tapering(self):
        """Collateral-constrained low-price ceiling is also tapered."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-TAPER",
            order_dollars=10.0, # At 10c mid -> raw count = 100 contracts
            max_low_price_contracts=6,
            low_price_threshold_cents=25,
        )
        # Normal ceiling limits to 6 contracts
        assert bot.calculate_order_size(10.0, taper_factor=1.0) == 6

        # Tapered ceiling: 6 * 0.5 = 3 contracts
        assert bot.calculate_order_size(10.0, taper_factor=0.5) == 3

        # Tapered ceiling: 6 * 0.2 = 1 contract
        assert bot.calculate_order_size(10.0, taper_factor=0.2) == 1

    @pytest.mark.asyncio
    async def test_tapering_reduce_only_safeguard(self):
        """In late taper (alpha < 0.50), quote only on the unwinding side if holding inventory."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-REDUCE",
            gamma=1.0,
            min_spread=6,
            order_size=2,
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        # 2250s to close -> alpha = 450 / 1800 = 0.25 (< 0.50)
        bot._market_close_time = now_dt + datetime.timedelta(seconds=2250)
        bot._refresh_market_metadata = AsyncMock()

        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_realized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_unrealized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={})
        bot.inv_manager.get_last_fill_time = MagicMock(return_value=0.0)
        bot.inv_manager.update_orderbook_mid = MagicMock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot._update_quotes = AsyncMock()

        # 1. Mild Long Inventory (inventory = +1 < tapered hedge threshold 2):
        # should suppress BUY YES (optimal_bid is None) while keeping resting maker ASK
        bot.inv_manager.get_position = MagicMock(return_value=1)
        await bot._tick()
        bot._update_quotes.assert_called_once()
        bid, ask = bot._update_quotes.call_args[0]
        assert bid is None
        assert ask is not None
        assert ask >= 50

        # 2. Heavy Long Inventory (inventory = +3 >= tapered hedge threshold 2):
        # should trigger active hedge (optimal_bid is None, ask crosses best_bid to exit)
        bot._update_quotes.reset_mock()
        bot.inv_manager.get_position = MagicMock(return_value=3)
        await bot._tick()
        bot._update_quotes.assert_called_once()
        bid, ask = bot._update_quotes.call_args[0]
        assert bid is None
        assert ask == 48  # Crossed best_bid

        # 3. Mild Short Inventory (inventory = -1): should suppress SELL YES (optimal_ask is None)
        bot._update_quotes.reset_mock()
        bot.inv_manager.get_position = MagicMock(return_value=-1)
        await bot._tick()
        bot._update_quotes.assert_called_once()
        bid, ask = bot._update_quotes.call_args[0]
        assert ask is None
        assert bid is not None
        assert bid <= 50

        # 4. Flat Inventory (inventory = 0): quotes both sides with tapered size
        bot._update_quotes.reset_mock()
        bot.inv_manager.get_position = MagicMock(return_value=0)
        await bot._tick()
        bot._update_quotes.assert_called_once()
        bid, ask = bot._update_quotes.call_args[0]
        assert bid is not None
        assert ask is not None
        assert bid < 50 < ask

    @pytest.mark.asyncio
    async def test_tapering_gamma_skew_amplification(self):
        """Pre-blackout tapering increases gamma to shade reservation price towards flattening."""
        bot_normal = AvellanedaStoikovBot(
            ticker="KXTEST-GAMMA",
            gamma=1.0,
            min_spread=6,
            inventory_taper_enabled=False,
        )
        bot_tapered = AvellanedaStoikovBot(
            ticker="KXTEST-GAMMA",
            gamma=1.0,
            min_spread=6,
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        # alpha = 0.25 (75% through taper) -> taper_gamma_multiplier = 1.0 + 0.75 * 1.5 = 2.125
        bot_tapered._market_close_time = now_dt + datetime.timedelta(seconds=2250)

        for b in (bot_normal, bot_tapered):
            b._refresh_market_metadata = AsyncMock()
            b.inv_manager.get_balance = MagicMock(return_value=10000)
            b.inv_manager.get_position = MagicMock(return_value=4)
            b.inv_manager.get_realized_pnl = MagicMock(return_value=0.0)
            b.inv_manager.get_unrealized_pnl = MagicMock(return_value=0.0)
            b.inv_manager.get_pnl_summary = MagicMock(return_value={})
            b.inv_manager.get_last_fill_time = MagicMock(return_value=0.0)
            b.inv_manager.update_orderbook_mid = MagicMock()
            b.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
            b.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
            b._update_quotes = AsyncMock()

        # Normal bot:
        await bot_normal._tick()
        bid_norm, ask_norm = bot_normal._update_quotes.call_args[0]

        # Tapered bot:
        await bot_tapered._tick()
        bid_tap, ask_tap = bot_tapered._update_quotes.call_args[0]

        # Long inventory: tapered bid is suppressed (None) due to reduce-only,
        # and ask price is shifted lower to incentivize exit fills
        assert bid_tap is None
        assert ask_tap <= ask_norm

    def test_calculate_order_size_taper_applied_after_hard_cap(self):
        """Taper factor must scale the capped baseline rather than the untapered raw count."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-CAP",
            order_dollars=20.0,  # At 50c mid -> raw_count = 40 contracts
            max_order_contracts=20,
        )
        # Baseline: capped at 20 contracts
        assert bot.calculate_order_size(50.0, taper_factor=1.0) == 20

        # At 50% taper factor, must scale the 20-contract cap down to 10 contracts (not 20)
        assert bot.calculate_order_size(50.0, taper_factor=0.5) == 10

        # Fixed order_size fallback also scales the capped baseline
        bot_fallback = AvellanedaStoikovBot(
            ticker="KXTEST-FALLBACK",
            order_dollars=None,
            order_size=10,
            max_order_contracts=6,
        )
        assert bot_fallback.calculate_order_size(50.0, taper_factor=1.0) == 6
        assert bot_fallback.calculate_order_size(50.0, taper_factor=0.5) == 3

    @pytest.mark.asyncio
    async def test_tapering_surviving_quote_capped_at_inventory(self):
        """Surviving quote in late taper must be capped at abs(inventory) to prevent position flip."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-FLIP",
            gamma=1.0,
            min_spread=6,
            order_size=10,
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        # alpha = 0.25 (< 0.50) -> quote_size = max(1, floor(10 * 0.25)) = 2 contracts
        bot._market_close_time = now_dt + datetime.timedelta(seconds=2250)
        bot._refresh_market_metadata = AsyncMock()

        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_realized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_unrealized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={})
        bot.inv_manager.get_last_fill_time = MagicMock(return_value=0.0)
        bot.inv_manager.update_orderbook_mid = MagicMock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot._update_quotes = AsyncMock()

        # 1. Long Inventory (+1 contract):
        # Even if quote_size is 2, surviving ask size must be capped at 1 contract!
        bot.inv_manager.get_position = MagicMock(return_value=1)
        await bot._tick()
        assert bot._current_ask_quote_size == 1

        # 2. Short Inventory (-1 contract):
        # Even if quote_size is 2, surviving bid size must be capped at 1 contract!
        bot.inv_manager.get_position = MagicMock(return_value=-1)
        await bot._tick()
        assert bot._current_bid_quote_size == 1

    def test_cap_quote_size_to_inventory_direct(self):
        """Direct unit tests for _cap_quote_size_to_inventory helper across integer and fractional positions."""
        bot = AvellanedaStoikovBot(ticker="KXTEST-CAP")

        # 1. Integer positions preserve int type and exact min bounds
        assert bot._cap_quote_size_to_inventory(10, 5) == 5
        assert isinstance(bot._cap_quote_size_to_inventory(10, 5), int)
        assert bot._cap_quote_size_to_inventory(2, 5) == 2
        assert bot._cap_quote_size_to_inventory(10, -3) == 3
        assert isinstance(bot._cap_quote_size_to_inventory(10, -3), int)

        # 2. Fractional positions return float bounded strictly by abs(inventory)
        assert bot._cap_quote_size_to_inventory(10, 1.5) == 1.5
        assert bot._cap_quote_size_to_inventory(10, -1.5) == 1.5
        assert bot._cap_quote_size_to_inventory(10, 0.5) == 0.5
        assert bot._cap_quote_size_to_inventory(10, -0.75) == 0.75
        assert bot._cap_quote_size_to_inventory(10, Decimal("1.5")) == 1.5

        # 3. Precision rounding never overshoots inventory
        assert bot._cap_quote_size_to_inventory(10, 1.559) <= 1.559
        assert bot._cap_quote_size_to_inventory(10, 1.551) <= 1.551

        # 4. Zero, negative quote size, or non-finite safe fallback
        assert bot._cap_quote_size_to_inventory(10, 0) == 10
        assert bot._cap_quote_size_to_inventory(10, None) == 10
        assert bot._cap_quote_size_to_inventory(10, float("nan")) == 10

    @pytest.mark.asyncio
    async def test_fractional_inventory_in_update_quotes(self):
        """_update_quotes preserves fractional quote sizes and places orders with exact contract count."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-FRAC",
            order_size=10,
        )
        bot.om.place_order = AsyncMock(return_value="order-123")
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=1.5)
        bot._current_ask_quote_size = 1.5
        bot._current_bid_quote_size = 10

        await bot._update_quotes(None, 52)
        bot.om.place_order.assert_called_once_with(
            ticker="KXTEST-FRAC",
            side="yes",
            action="sell",
            count=1.5,
            price=52,
        )
        assert bot.current_ask_size == 1.5

        # Test bid side with fractional size
        bot.om.place_order.reset_mock()
        bot.current_ask_id = None
        bot.inv_manager.get_position = MagicMock(return_value=-0.5)
        bot._current_bid_quote_size = 0.5
        await bot._update_quotes(48, None)
        bot.om.place_order.assert_called_once_with(
            ticker="KXTEST-FRAC",
            side="yes",
            action="buy",
            count=0.5,
            price=48,
        )
        assert bot.current_bid_size == 0.5

    @pytest.mark.asyncio
    async def test_session_risk_safeguard_hedge_capped_size(self):
        """Session-risk safeguard hedge branch caps quote size to open inventory."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-RISK-HEDGE",
            gamma=1.0,
            order_size=10,
            max_hedge_inventory=10,
            max_session_fees_cents=300,
        )
        bot._refresh_market_metadata = AsyncMock()
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_realized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_unrealized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_last_fill_time = MagicMock(return_value=0.0)
        bot.inv_manager.update_orderbook_mid = MagicMock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot._update_quotes = AsyncMock()

        # Trigger session risk via fee churn: 500c >= 300c
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={
            "session_fees_cents": 500,
            "total_fees_cents": 500,
            "session_realized_pnl_cents": 0.0,
            "realized_pnl_cents": 0.0,
            "unrealized_pnl_cents": 0.0,
        })

        # 1. Long Inventory (6 contracts >= hedge threshold 5): ask size capped at 6, not 10!
        bot.inv_manager.get_position = MagicMock(return_value=6)
        await bot._tick()
        assert bot._current_ask_quote_size == 6
        bot._update_quotes.assert_called_once_with(None, 48)

        # 2. Fractional Short Inventory (-5.5 contracts <= -5): bid size capped at 5.5, not 10!
        bot._update_quotes.reset_mock()
        bot.inv_manager.get_position = MagicMock(return_value=-5.5)
        await bot._tick()
        assert bot._current_bid_quote_size == 5.5
        bot._update_quotes.assert_called_once_with(52, None)

    @pytest.mark.asyncio
    async def test_extreme_price_collar_safeguard_hedge_capped_size(self):
        """Extreme price collar safeguard hedge branch caps quote size to open inventory."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-COLLAR-HEDGE",
            gamma=1.0,
            order_size=10,
            max_hedge_inventory=10,
            min_mid_price=20.0,
            max_mid_price=80.0,
        )
        bot._refresh_market_metadata = AsyncMock()
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_realized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_unrealized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={"total_fees_paid_cents": 0, "realized_pnl_cents": 0.0})
        bot.inv_manager.get_last_fill_time = MagicMock(return_value=0.0)
        bot.inv_manager.update_orderbook_mid = MagicMock()
        bot._update_quotes = AsyncMock()

        # Mid-price = 11.0c, breach below min_mid_price (20.0c)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(10, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(12, 10))

        # 1. Short Inventory (-7 contracts <= -5): bid size capped at 7, not 10!
        bot.inv_manager.get_position = MagicMock(return_value=-7)
        await bot._tick()
        assert bot._current_bid_quote_size == 7
        bot._update_quotes.assert_called_once_with(12, None)

        # 2. Fractional Long Inventory (6.5 contracts >= 5): ask size capped at 6.5, not 10!
        bot._update_quotes.reset_mock()
        bot.inv_manager.get_position = MagicMock(return_value=6.5)
        await bot._tick()
        assert bot._current_ask_quote_size == 6.5
        bot._update_quotes.assert_called_once_with(None, 10)

    @pytest.mark.asyncio
    async def test_tapering_fractional_position_capped_in_tick(self):
        """Late taper reduce-only mode caps fractional position properly."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-FRAC-TAPER",
            gamma=1.0,
            min_spread=6,
            order_size=10,
            inventory_taper_enabled=True,
            inventory_taper_window_seconds=1800,
            expiration_blackout_seconds=1800,
        )
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        # alpha = 0.25 (< 0.50)
        bot._market_close_time = now_dt + datetime.timedelta(seconds=2250)
        bot._refresh_market_metadata = AsyncMock()
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        bot.inv_manager.get_realized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_unrealized_pnl = MagicMock(return_value=0.0)
        bot.inv_manager.get_pnl_summary = MagicMock(return_value={})
        bot.inv_manager.get_last_fill_time = MagicMock(return_value=0.0)
        bot.inv_manager.update_orderbook_mid = MagicMock()
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot._update_quotes = AsyncMock()

        # Fractional long inventory of +1.5 contracts
        bot.inv_manager.get_position = MagicMock(return_value=1.5)
        await bot._tick()
        assert bot._current_ask_quote_size == 1.5

        # Fractional short inventory of -0.5 contracts
        bot.inv_manager.get_position = MagicMock(return_value=-0.5)
        await bot._tick()
        assert bot._current_bid_quote_size == 0.5

    @pytest.mark.asyncio
    async def test_concurrent_fill_during_cancellation_caps_quote_size(self):
        """Fills arriving during cancellation re-cap the replacement quote or abort if flattened."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-CONCURRENT",
            order_size=10,
            inventory_taper_enabled=True,
        )
        bot._is_reduce_only = True
        bot._current_taper_factor = 0.25
        bot._current_ask_quote_size = 5
        bot.current_ask_id = "old-ask-1"
        bot.current_ask_price = 55
        bot.current_ask_size = 10
        bot.om.place_order = AsyncMock(return_value="new-ask-1")

        # Scenario 1: Partial fill arrives while cancellation is in flight (inventory drops from 5 to 2)
        async def fake_cancel_partial(order_id):
            bot.inv_manager.get_position.return_value = 2
            return True

        bot.om.cancel_order = AsyncMock(side_effect=fake_cancel_partial)
        bot.inv_manager.get_position = MagicMock(return_value=5)

        await bot._update_quotes(None, 52)
        # Order placement must use fresh inventory (2) instead of the stale snapshot (5)!
        bot.om.place_order.assert_called_once_with(
            ticker="KXTEST-CONCURRENT",
            side="yes",
            action="sell",
            count=2,
            price=52,
        )
        assert bot.current_ask_size == 2

        # Scenario 2: Complete fill arrives while cancellation is in flight (inventory drops to 0)
        bot.om.place_order.reset_mock()
        bot.current_ask_id = "old-ask-2"
        bot.current_ask_price = 55
        bot.current_ask_size = 2
        bot._current_ask_quote_size = 2

        async def fake_cancel_complete(order_id):
            bot.inv_manager.get_position.return_value = 0
            return True

        bot.om.cancel_order = AsyncMock(side_effect=fake_cancel_complete)
        bot.inv_manager.get_position = MagicMock(return_value=2)

        await bot._update_quotes(None, 52)
        # Order placement must be completely skipped because position is flat!
        bot.om.place_order.assert_not_called()
        assert bot.current_ask_id is None

    @pytest.mark.asyncio
    async def test_opposing_quote_withdrawal_confirmed_before_hedge(self):
        """Hedge quote placement is aborted if opposing quote withdrawal fails."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-OPPOSING",
            order_size=10,
        )
        bot._is_reduce_only = True
        # Active opposing BID quote on the exchange
        bot.current_bid_id = "active-bid-1"
        bot.current_bid_price = 45
        bot.current_bid_size = 5
        bot._current_ask_quote_size = 5

        # Cancellation fails (e.g. timeout or API error)
        bot.om.cancel_order = AsyncMock(return_value=False)
        bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
        bot.om.place_order = AsyncMock(return_value="hedge-ask-1")
        bot.inv_manager.get_position = MagicMock(return_value=5)

        # Single-sided long hedge: new_bid is None (halt bids), new_ask is 52 (cross ask)
        await bot._update_quotes(None, 52)

        # Because opposing BID withdrawal was not confirmed, ASK placement must be aborted!
        bot.om.place_order.assert_not_called()
        assert bot.current_bid_id == "active-bid-1"
        assert bot.current_ask_id is None

    @pytest.mark.asyncio
    async def test_failed_cancellation_oversized_quote_handling(self):
        """Failed cancellation retains quote ID to prevent duplicates and triggers reconciliation."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-FAILED-CANCEL",
            order_size=10,
        )
        bot.current_ask_id = "old-ask-oversized"
        bot.current_ask_price = 55
        bot.current_ask_size = 10
        bot._current_ask_quote_size = 2

        bot.om.cancel_order = AsyncMock(return_value=False)
        bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
        bot.om.place_order = AsyncMock(return_value="new-ask-1")
        bot.inv_manager.get_position = MagicMock(return_value=2)

        await bot._update_quotes(None, 52)

        # Retains active quote ID to block duplicate placement
        assert bot.current_ask_id == "old-ask-oversized"
        bot.om.place_order.assert_not_called()
        # Dispatched reconciliation task in background with target_client_order_id
        await asyncio.sleep(0.01)
        bot.om.reconcile_resting_orders.assert_called_once_with(
            ticker="KXTEST-FAILED-CANCEL",
            target_client_order_id="old-ask-oversized",
        )

    @pytest.mark.asyncio
    async def test_pending_fill_processing_drains_or_defers_quote_update(self):
        """_update_quotes drains in-flight fills or defers quote replacement if fill is still processing."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-PENDING-FILL",
            order_size=10,
        )
        bot.om.place_order = AsyncMock(return_value="order-123")
        bot.om.cancel_order = AsyncMock(return_value=True)

        # Case 1: Pending fill drains to 0 within the wait loop
        bot.inv_manager._pending_fills = 1
        async def drain_fills():
            await asyncio.sleep(0.01)
            bot.inv_manager._pending_fills = 0
            bot.inv_manager.get_position = MagicMock(return_value=3)

        asyncio.create_task(drain_fills())
        bot.inv_manager.get_position = MagicMock(return_value=5)
        bot._current_ask_quote_size = 5
        bot._is_reduce_only = True

        await bot._update_quotes(None, 52)
        # Should have waited for _pending_fills to drain to 0, then placed ask for fresh inventory (3)
        bot.om.place_order.assert_called_once_with(
            ticker="KXTEST-PENDING-FILL",
            side="yes",
            action="sell",
            count=3,
            price=52,
        )

        # Case 2: Pending fill does NOT drain (stays > 0) -> quote placement deferred!
        bot.om.place_order.reset_mock()
        bot.inv_manager._pending_fills = 2
        await bot._update_quotes(None, 52)
        bot.om.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_crossing_hedge_invokes_reduce_only_ioc(self):
        """Crossing exit hedge orders pass exchange-enforced reduce_only=True and IOC."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-CROSS-HEDGE",
            order_size=10,
            max_hedge_inventory=10,
        )
        bot.om.place_order = AsyncMock(return_value="hedge-ioc-123")
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=6)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot._is_reduce_only = True
        bot._current_ask_quote_size = 6

        # Cross best bid (48c) to exit 6 long contracts
        await bot._update_quotes(None, 48)

        bot.om.place_order.assert_called_once_with(
            ticker="KXTEST-CROSS-HEDGE",
            side="yes",
            action="sell",
            count=6,
            price=48,
            reduce_only=True,
            time_in_force="immediate_or_cancel",
        )
        # IOC crossing order does not leave resting quote state
        assert bot.current_ask_id is None

    @pytest.mark.asyncio
    async def test_passive_resting_taper_quote_uses_gtc_without_reduce_only(self):
        """Passive resting quotes (not crossing) remain GTC without reduce_only to avoid Kalshi rejection."""
        bot = AvellanedaStoikovBot(
            ticker="KXTEST-PASSIVE-TAPER",
            order_size=10,
            inventory_taper_enabled=True,
        )
        bot.om.place_order = AsyncMock(return_value="passive-gtc-123")
        bot.om.cancel_order = AsyncMock(return_value=True)
        bot.inv_manager.get_position = MagicMock(return_value=2)
        bot.ob_manager.get_best_bid = MagicMock(return_value=(48, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(52, 10))
        bot._is_reduce_only = True
        bot._current_ask_quote_size = 2

        # Passive ask @ 51c (above best bid 48c -> not crossing)
        await bot._update_quotes(None, 51)

        bot.om.place_order.assert_called_once_with(
            ticker="KXTEST-PASSIVE-TAPER",
            side="yes",
            action="sell",
            count=2,
            price=51,
        )
        # Passive quote rests on the book
        assert bot.current_ask_id == "passive-gtc-123"
        assert bot.current_ask_price == 51
