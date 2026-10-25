import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from strategy.market_maker import AvellanedaStoikovBot


def test_collateral_constrained_sizing_on_low_price_contracts():
    """
    Verify that low-price contracts (<= low_price_threshold_cents) have contract counts capped
    at max_low_price_contracts to prevent short/NO collateral exhaustion.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXNBAGAME-26OCT25TORMIN-MIN",
        order_dollars=5.0,
        max_order_contracts=100,
        max_low_price_contracts=20,
        low_price_threshold_cents=25,
    )

    # 1. Normal price (50c): raw count = ceil(500 / 50) = 10 contracts.
    # Price 50c > 25c, so no low-price capping applies -> 10 contracts.
    assert bot.calculate_order_size(mid_price=50.0) == 10

    # 2. Low price (16c): raw count = ceil(500 / 16) = 32 contracts.
    # Price 16c <= 25c, capped at max_low_price_contracts (20) -> 20 contracts.
    assert bot.calculate_order_size(mid_price=16.0) == 20

    # 3. Very low price (5c): raw count = ceil(500 / 5) = 100 contracts.
    # Price 5c <= 25c, capped at max_low_price_contracts (20) -> 20 contracts.
    assert bot.calculate_order_size(mid_price=5.0) == 20

    # 4. Disabled constraint (max_low_price_contracts=0) allows full raw sizing up to max_order_contracts.
    bot_unconstrained = AvellanedaStoikovBot(
        ticker="KXNBAGAME-26OCT25TORMIN-MIN",
        order_dollars=5.0,
        max_order_contracts=100,
        max_low_price_contracts=0,
    )
    assert bot_unconstrained.calculate_order_size(mid_price=16.0) == 32
    assert bot_unconstrained.calculate_order_size(mid_price=5.0) == 100


@pytest.mark.asyncio
async def test_momentum_skew_aggression_and_drift_retreat():
    """
    Verify that momentum skew increases effective gamma when normalized inventory > 1.0,
    and applies an adverse drift penalty to shift quotes safely away from momentum runs.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXNBAGAME-26OCT25TORMIN-MIN",
        gamma=0.5,
        min_spread=4,
        order_size=1,
        momentum_skew_enabled=True,
        momentum_gamma_factor=0.5,
        max_gamma_multiplier=2.5,
        price_velocity_threshold_cents=20.0,  # Do not trip fast-market during test
    )

    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot._update_quotes = AsyncMock()

    # 1. Inventory = 3 (normalized_q = 3.0 > 1.0).
    # excess_q = 2.0 -> gamma_scale = 1.0 + (0.5 * 2.0) = 2.0 -> effective_gamma = 1.0.
    # Without drift, ResPrice = 50.0 - (3.0 * 1.0) = 47.0.
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))
    bot.inv_manager.get_position = MagicMock(return_value=3)

    await bot._tick()
    # Spread = 4 -> Bid = floor(47 - 2) = 45, Ask = ceil(47 + 2) = 49
    bot._update_quotes.assert_called_with(45, 49)

    # 2. Simulate adverse drift while holding long inventory:
    # Price dropped from 50c to 46c (drift = -4.0c).
    # adverse_drift_shift = abs(-4.0) * 0.5 = 2.0c.
    # New mid = 46.0c.
    # ResPrice = 46.0 - (3.0 * 1.0) - 2.0 = 41.0c.
    # Bid = floor(41 - 2) = 39, Ask = ceil(41 + 2) = 43
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(47, 10))
    await bot._tick()
    bot._update_quotes.assert_called_with(39, 43)


@pytest.mark.asyncio
async def test_auto_sweep_shard0_idle_capital():
    """
    Verify that ensure_shard_collateral automatically sweeps idle cash from Shard 0 to active sports shard.
    """
    bot = AvellanedaStoikovBot(
        ticker="KXNBAGAME-26OCT25TORMIN-MIN",
        auto_sweep_shard0_enabled=True,
        shard0_reserve_dollars=1.0,
    )
    bot.exchange_index = 3

    # Shard 3 has $20.00 (meets $15 requirement). Shard 0 has $9.50.
    # available_sweep = 9.50 - 1.00 = $8.50.
    initial_balances = {
        "shard_balances": {
            0: 9.50,
            3: 20.00,
        }
    }
    settled_balances = {
        "shard_balances": {
            0: 1.00,
            3: 28.50,
        }
    }

    call_count = 0

    def mock_get_balances():
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return initial_balances
        return settled_balances

    bot.om.get_portfolio_balances = MagicMock(side_effect=mock_get_balances)
    bot.om.transfer_intra_exchange = MagicMock(return_value=True)

    result = await bot.ensure_shard_collateral(min_required_dollars=15.0)

    assert result is True
    bot.om.transfer_intra_exchange.assert_called_once_with(
        source_shard=0,
        destination_shard=3,
        amount_dollars=8.50,
    )


def test_max_gamma_multiplier_lower_bound_clamping(monkeypatch):
    """
    Verify that max_gamma_multiplier enforces a minimum lower bound of 1.0
    both when passed explicitly and when defaulting to MAX_GAMMA_MULTIPLIER.
    """
    # 1. Explicit override < 1.0 clamped to 1.0
    bot_explicit = AvellanedaStoikovBot(
        ticker="KXNBAGAME-26OCT25TORMIN-MIN",
        max_gamma_multiplier=0.5,
    )
    assert bot_explicit.max_gamma_multiplier == 1.0

    # 2. Config fallback with environment < 1.0 clamped to 1.0
    monkeypatch.setattr("strategy.market_maker.MAX_GAMMA_MULTIPLIER", 0.75)
    bot_fallback = AvellanedaStoikovBot(
        ticker="KXNBAGAME-26OCT25TORMIN-MIN",
        max_gamma_multiplier=None,
    )
    assert bot_fallback.max_gamma_multiplier == 1.0

    # 3. Valid multiplier >= 1.0 preserved
    bot_valid = AvellanedaStoikovBot(
        ticker="KXNBAGAME-26OCT25TORMIN-MIN",
        max_gamma_multiplier=3.0,
    )
    assert bot_valid.max_gamma_multiplier == 3.0
