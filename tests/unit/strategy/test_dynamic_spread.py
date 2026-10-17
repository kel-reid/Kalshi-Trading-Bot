"""
Unit Tests for Dynamic Volatility & Fee-Aware Spread Engine
"""

import math
import time
import pytest

from strategy.dynamic_spread import (
    calculate_fee_cents,
    calculate_spread_floor_cents,
    calculate_optimal_spread,
    RollingVolatilityEstimator,
    DEFAULT_KALSHI_FEE_RATE,
    DEFAULT_TARGET_EDGE_CENTS,
)


class TestFeeCalculations:
    """Tests for single-leg and round-trip fee calculations."""

    def test_fee_symmetric_at_fifty_cents(self):
        """Fee should peak symmetrically at 50 cents."""
        fee_50 = calculate_fee_cents(50.0)
        # 0.07 * 50 * 50 / 100 = 1.75c
        assert fee_50 == pytest.approx(1.75, abs=0.01)

        fee_20 = calculate_fee_cents(20.0)
        fee_80 = calculate_fee_cents(80.0)
        assert fee_20 == pytest.approx(fee_80, abs=0.01)
        assert fee_20 == pytest.approx(1.12, abs=0.01)

    def test_fee_bounds_extreme_prices(self):
        """Valid prices at the collars should return strictly positive fees."""
        fee_1 = calculate_fee_cents(1.0)
        fee_99 = calculate_fee_cents(99.0)
        assert fee_1 > 0.0
        assert fee_1 == pytest.approx(fee_99, abs=0.01)

    @pytest.mark.parametrize("invalid_price", [0.0, 100.0, -5.0, 150.0, float("nan"), float("inf"), None, True, False])
    def test_invalid_price_returns_zero_fee(self, invalid_price):
        """Invalid or non-exchange prices should return 0 fee safely."""
        assert calculate_fee_cents(invalid_price) == 0.0


class TestSpreadFloor:
    """Tests for minimum fee-clearing spread floors."""

    def test_spread_floor_at_midpoint(self):
        """Spread floor at 50c covers 2 * 1.75c = 3.5c fee + 2c edge -> 6c."""
        floor = calculate_spread_floor_cents(50.0, target_edge_cents=2.0)
        assert floor == 6

    def test_spread_floor_at_tail_prices(self):
        """Wings (20c/80c) have lower fees (2 * 1.12c = 2.24c), so floor is tighter."""
        floor_20 = calculate_spread_floor_cents(20.0, target_edge_cents=1.5)
        # 2.24 + 1.5 = 3.74 -> ceil = 4
        assert floor_20 == 4

    def test_spread_floor_respects_minimum_bound(self):
        """Even with 0 fee, spread floor respects min_floor_cents."""
        floor = calculate_spread_floor_cents(1.0, target_edge_cents=0.0, min_floor_cents=3)
        assert floor >= 3


class TestRollingVolatilityEstimator:
    """Tests for sliding-window realized volatility estimation."""

    def test_insufficient_samples_uses_baseline(self):
        """Fewer than min_samples returns the configured baseline."""
        estimator = RollingVolatilityEstimator(window_seconds=30.0, min_samples=3, baseline_volatility=1.5)
        estimator.update(50.0)
        assert estimator.get_realized_volatility() == 1.5
        estimator.update(51.0)
        assert estimator.get_realized_volatility() == 1.5

    def test_sample_standard_deviation_calculation(self):
        """With >= 3 samples, computes standard deviation correctly."""
        estimator = RollingVolatilityEstimator(window_seconds=30.0, min_samples=3)
        t = 1000.0
        estimator.update(10.0, timestamp=t)
        estimator.update(20.0, timestamp=t + 1)
        estimator.update(30.0, timestamp=t + 2)
        # Values: 10, 20, 30. Mean: 20. Variance: ((10-20)^2 + 0 + (30-20)^2) / 2 = 200/2 = 100. Stdev = 10.0.
        vol = estimator.get_realized_volatility(now=t + 2)
        assert vol == pytest.approx(10.0, abs=0.01)

    def test_sliding_window_pruning(self):
        """Observations outside window_seconds are pruned."""
        estimator = RollingVolatilityEstimator(window_seconds=10.0, min_samples=2)
        t0 = 100.0
        estimator.update(10.0, timestamp=t0)
        estimator.update(12.0, timestamp=t0 + 5)
        assert estimator.sample_count() == 2

        # Advance beyond 10s window from t0
        estimator.update(14.0, timestamp=t0 + 15)
        assert estimator.sample_count() == 2  # t0 was pruned, only t0+5 and t0+15 remain

    def test_reset_clears_history(self):
        """Reset clears all historical samples."""
        estimator = RollingVolatilityEstimator()
        estimator.update(50.0)
        estimator.update(51.0)
        estimator.reset()
        assert estimator.sample_count() == 0


class TestOptimalSpreadCalculation:
    """Tests for the combined Avellaneda-Stoikov dynamic spread."""

    def test_spread_widens_with_high_volatility(self):
        """Higher realized volatility should expand the quoting spread."""
        low_vol_spread = calculate_optimal_spread(
            mid_price_cents=50.0,
            realized_volatility_cents=0.5,
            book_depth=20,
        )
        high_vol_spread = calculate_optimal_spread(
            mid_price_cents=50.0,
            realized_volatility_cents=4.0,
            book_depth=20,
        )
        assert high_vol_spread > low_vol_spread

    def test_spread_tightens_with_deep_book(self):
        """Deeper book / higher liquidity density should tighten the spread."""
        thin_book_spread = calculate_optimal_spread(
            mid_price_cents=20.0,
            realized_volatility_cents=0.5,
            book_depth=1,
            min_spread_cents=2,
        )
        deep_book_spread = calculate_optimal_spread(
            mid_price_cents=20.0,
            realized_volatility_cents=0.5,
            book_depth=50,
            min_spread_cents=2,
        )
        assert deep_book_spread <= thin_book_spread

    def test_spread_never_below_fee_floor(self):
        """Optimal spread is strictly bounded from below by the fee floor."""
        floor = calculate_spread_floor_cents(50.0, target_edge_cents=2.0)
        # Even with 0 volatility and massive depth:
        spread = calculate_optimal_spread(
            mid_price_cents=50.0,
            realized_volatility_cents=0.0,
            book_depth=1000,
            target_edge_cents=2.0,
        )
        assert spread >= floor

    def test_spread_capped_at_max(self):
        """Extreme volatility cannot exceed max_spread_cents."""
        spread = calculate_optimal_spread(
            mid_price_cents=50.0,
            realized_volatility_cents=50.0,  # massive volatility
            max_spread_cents=12,
        )
        assert spread <= 12


class TestMarketMakerDynamicSpreadIntegration:
    """Integration tests verifying MarketMaker quoting behavior with dynamic spread enabled."""

    @pytest.mark.asyncio
    async def test_market_maker_dynamic_spread_quoting(self):
        """MarketMaker should compute dynamic spread and quote around midpoint."""
        from unittest.mock import AsyncMock, MagicMock
        from strategy.market_maker import AvellanedaStoikovBot

        bot = AvellanedaStoikovBot(
            ticker="TEST_TICKER",
            gamma=1.0,
            dynamic_spread_enabled=True,
            target_edge_cents=2.0,
            min_dynamic_spread_cents=2,
            max_dynamic_spread_cents=15,
            order_size=1,
        )

        bot.inv_manager.get_position = MagicMock(return_value=0)
        bot.inv_manager.get_balance = MagicMock(return_value=10000)
        # Midpoint = 50.0c, depth = 20
        bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
        bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))
        bot._update_quotes = AsyncMock()

        await bot._tick()

        # At mid 50c, fee floor = ceil(2 * 1.75 + 2.0) = 6c.
        # Initial baseline volatility = 1.0c. Optimal spread should be >= 6c.
        bot._update_quotes.assert_called_once()
        placed_bid, placed_ask = bot._update_quotes.call_args[0]
        assert placed_bid is not None and placed_ask is not None
        spread = placed_ask - placed_bid
        assert spread >= 6
        assert placed_bid < 50.0 < placed_ask

    @pytest.mark.asyncio
    async def test_market_maker_rotation_resets_volatility(self):
        """Market rotation should clear historical midpoint volatility samples."""
        from unittest.mock import AsyncMock, MagicMock
        from strategy.market_maker import AvellanedaStoikovBot

        bot = AvellanedaStoikovBot(
            ticker="OLD_TICKER",
            dynamic_spread_enabled=True,
        )
        bot.volatility_estimator.update(50.0)
        bot.volatility_estimator.update(52.0)
        assert bot.volatility_estimator.sample_count() == 2

        # Mock rotation dependencies
        bot.inv_manager.get_position = MagicMock(return_value=0)
        bot._cancel_all_quotes = AsyncMock()
        bot.om.reconcile_resting_orders = AsyncMock()
        bot.om.record_pnl_snapshot_async = AsyncMock()
        bot.ob_manager.unsubscribe = AsyncMock()
        bot.ob_manager.subscribe = AsyncMock()

        success = await bot.rotate_market("NEW_TICKER")
        assert success is True
        assert bot.ticker == "NEW_TICKER"
        assert bot.volatility_estimator.sample_count() == 0
