"""
Unit tests for Late-Session Dynamic Inventory Tapering Safeguards.
Verifies pre-blackout sizing scaling, gamma skew boosting, reduce-only quote gating,
and invariant preservation leading into market expiration.
"""

import datetime
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
