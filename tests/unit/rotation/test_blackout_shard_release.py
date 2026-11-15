import datetime
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategy.market_maker import AvellanedaStoikovBot


@pytest.mark.asyncio
@pytest.mark.parametrize("race_point", ["swap", "sweep"])
async def test_rotation_rechecks_before_and_after_sweep(race_point):
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot.exchange_index = 3
    bot.auto_sweep_reverse_enabled = True
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot._verified_ticker_positions = AsyncMock(return_value={})
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()
    async def race(**kwargs):
        bot.inv_manager._fill_count += 1
    bot.sweep_shard_to_shard0 = AsyncMock(side_effect=race if race_point == "sweep" else None)
    if race_point == "swap":
        async def subscribe(tickers):
            if tickers == ["NEW"]:
                await race()
        bot.ob_manager.subscribe.side_effect = subscribe
    with patch("strategy.market_maker.get_market_exchange_index", return_value=0):
        assert await bot.rotate_market("NEW") is False
    assert bot.ticker == "OLD"
    if race_point == "swap":
        bot.sweep_shard_to_shard0.assert_not_awaited()
    else:
        bot.sweep_shard_to_shard0.assert_awaited_once()
    bot.ob_manager.subscribe.assert_any_await(["OLD"])


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [{3: Decimal('0.25')}, None])
async def test_blackout_does_not_retire_unverified_local_flat_position(positions):
    bot = AvellanedaStoikovBot(ticker="TEST", blackout_enabled=True, auto_rotate=False)
    bot._last_market_status_check = time.time()
    bot._market_close_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._verified_ticker_positions = AsyncMock(return_value=positions)
    bot.liquidate_inventory = AsyncMock(return_value=False)
    bot._update_quotes = AsyncMock()

    await bot._tick()

    assert not bot._market_inactive
    bot._update_quotes.assert_not_awaited()
    if positions:
        bot.liquidate_inventory.assert_awaited_once_with("TEST")


@pytest.mark.asyncio
async def test_liquidation_routes_each_opposing_position_to_its_own_shard():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.exchange_index = 0
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[
        {0: Decimal('1'), 3: Decimal('-0.25')},
        {3: Decimal('-0.25')},
        {3: Decimal('-0.25')},
        {},
    ])
    bot.om.place_order = AsyncMock(side_effect=['one', 'two'])
    bot.om.get_order_status = AsyncMock(side_effect=[
        {'status': 'executed', 'fill_count_fp': '1'},
        {'status': 'executed', 'fill_count_fp': '0.25'},
    ])
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))

    with patch('strategy.market_maker.asyncio.sleep', new=AsyncMock()):
        assert await bot.liquidate_inventory() is True

    orders = [call.kwargs for call in bot.om.place_order.await_args_list]
    assert [(o['exchange_index'], o['action'], o['count']) for o in orders] == [
        (0, 'sell', Decimal('1')), (3, 'buy', Decimal('0.25')),
    ]
    assert all(o['reduce_only'] and o['time_in_force'] == 'immediate_or_cancel' for o in orders)


@pytest.mark.asyncio
async def test_liquidation_aborts_if_another_shard_changes_during_order():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[
        {0: Decimal('1'), 3: Decimal('-1')}, {3: Decimal('-0.5')},
    ])
    bot.om.place_order = AsyncMock(return_value='order')
    bot.om.get_order_status = AsyncMock(return_value={'status': 'executed', 'fill_count_fp': '1'})
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))
    with patch('strategy.market_maker.asyncio.sleep', new=AsyncMock()):
        assert await bot.liquidate_inventory() is False
    assert bot.om.place_order.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled, snapshots, inactive', [
    (False, [{}], False),
    (True, [{}, None], False),
    (True, [{}, {3: Decimal('0.1')}], False),
    (True, [{}, {}], True),
])
async def test_blackout_requires_cancel_and_final_flat_snapshot(cancelled, snapshots, inactive):
    bot = AvellanedaStoikovBot(ticker='TEST', blackout_enabled=True, auto_rotate=False)
    bot._last_market_status_check = time.time()
    bot._market_close_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))
    bot._cancel_all_quotes = AsyncMock(return_value=cancelled)
    bot._verified_ticker_positions = AsyncMock(side_effect=snapshots)
    bot._update_quotes = AsyncMock()
    await bot._tick()
    assert bot._market_inactive is inactive
    bot._update_quotes.assert_not_awaited()


@pytest.mark.asyncio
async def test_blackout_rotates_only_after_exchange_verification():
    bot = AvellanedaStoikovBot(ticker='TEST', blackout_enabled=True, auto_rotate=True)
    bot._last_market_status_check = time.time()
    bot._market_close_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._verified_ticker_positions = AsyncMock(side_effect=[None, {}])
    bot.rotate_market = AsyncMock(return_value=True)
    with patch('strategy.market_maker.discover_active_market_async', new=AsyncMock(return_value='NEW')) as discover:
        await bot._tick()
        discover.assert_not_awaited()
        await bot._tick()
        discover.assert_awaited_once()
    bot.rotate_market.assert_awaited_once_with('NEW')


@pytest.mark.asyncio
async def test_inactive_local_flat_retries_failed_query_then_liquidates_exchange_exposure():
    bot = AvellanedaStoikovBot(ticker='TEST', auto_rotate=False)
    bot._market_inactive = True
    bot._last_inactive_retry = time.time() - bot._inactive_retry_interval - 1
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.hydrate = AsyncMock(return_value=False)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[
        RuntimeError('position query failed'), {3: Decimal('0.25')}, {},
    ])
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.om.place_order = AsyncMock(return_value='order')
    bot.om.get_order_status = AsyncMock(return_value={'status': 'executed', 'fill_count_fp': '0.25'})
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot._update_quotes = AsyncMock()

    with patch('strategy.market_maker.send_alert', new=AsyncMock()), \
         patch('strategy.market_maker.asyncio.sleep', new=AsyncMock()):
        await bot._tick()
        assert bot.inv_manager._fetch_ticker_positions.call_count == 1
        bot.om.place_order.assert_not_awaited()
        assert bot._market_inactive
        await bot._tick()
        assert bot.inv_manager._fetch_ticker_positions.call_count == 1
        bot._last_inactive_retry -= bot._inactive_retry_interval + 1
        await bot._tick()

    bot.om.place_order.assert_awaited_once_with(
        ticker='TEST', side='yes', action='sell', count=Decimal('0.25'), price=49,
        reduce_only=True, time_in_force='immediate_or_cancel', exchange_index=3,
    )
    assert bot._market_inactive
    bot._update_quotes.assert_not_awaited()


@pytest.mark.asyncio
async def test_inactive_local_flat_never_queries_or_orders_if_cancellation_unconfirmed():
    bot = AvellanedaStoikovBot(ticker='TEST', auto_rotate=False)
    bot._market_inactive = True
    bot._last_inactive_retry = time.time() - bot._inactive_retry_interval - 1
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=0)
    bot.inv_manager.hydrate = AsyncMock(return_value=False)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={3: Decimal('1')})
    bot._cancel_all_quotes = AsyncMock(return_value=False)
    bot.om.place_order = AsyncMock()

    with patch('strategy.market_maker.send_alert', new=AsyncMock()):
        await bot._tick()

    bot._cancel_all_quotes.assert_awaited_once_with(ticker='TEST')
    bot.inv_manager._fetch_ticker_positions.assert_not_called()
    bot.om.place_order.assert_not_awaited()
    assert bot._market_inactive