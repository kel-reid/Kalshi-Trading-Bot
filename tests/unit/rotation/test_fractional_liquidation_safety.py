import asyncio
import datetime
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from strategy.market_maker import AvellanedaStoikovBot


def setup_healthy_feed(bot):
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    bot._ws_task = asyncio.create_task(asyncio.Event().wait())


@pytest.mark.asyncio
async def test_flat_liquidation_refuses_pending_placement():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})
    bot.om._pending_client_ids.add("pending")
    assert await bot.liquidate_inventory() is False


@pytest.mark.asyncio
async def test_flat_liquidation_cancels_extra_tracked_order():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})
    bot.om.active_orders["extra"] = {"ticker": "OLD", "kalshi_order_id": "exchange"}
    bot.om.cancel_order = AsyncMock(return_value=False)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    assert await bot.liquidate_inventory() is False
    bot.om.cancel_order.assert_awaited_once_with("extra")


@pytest.mark.asyncio
@pytest.mark.parametrize("position,action", [("0.02", "sell"), ("-0.02", "buy"), ("1.25", "sell"), ("-1.25", "buy")])
async def test_fractional_liquidation_uses_exact_reduce_only_ioc(position, action):
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot.inv_manager.get_position = MagicMock(side_effect=[float(position), 0])
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[
        {0: Decimal(position)}, {},
    ])
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.place_order = AsyncMock(return_value="order")
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": str(abs(Decimal(position)))})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))

    with patch("strategy.market_maker.asyncio.sleep", new=AsyncMock()):
        assert await bot.liquidate_inventory() is True

    args = bot.om.place_order.await_args.kwargs
    assert Decimal(str(args["count"])) == abs(Decimal(position))
    assert args["action"] == action
    assert args["reduce_only"] is True
    assert args["time_in_force"] == "immediate_or_cancel"
    assert bot.inv_manager._fetch_ticker_positions.call_count >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [{0: Decimal("0.02")}, None])
async def test_rotation_rechecks_even_successful_liquidation(positions):
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.inv_manager.get_position = MagicMock(return_value=1)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value=positions)
    bot.liquidate_inventory = AsyncMock(return_value=True)
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()
    with patch("strategy.market_maker.send_alert", new=AsyncMock()), patch("strategy.market_maker.get_raw_market_status_async", new=AsyncMock(return_value="closed")):
        assert await bot.rotate_market("NEW") is False
    bot.ob_manager.unsubscribe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [0.02, -0.02, 1.25, -1.25])
async def test_blackout_does_not_place_unprotected_exit_quotes(position):
    bot = AvellanedaStoikovBot(ticker="OLD", blackout_enabled=True, expiration_blackout_seconds=1800)
    bot._last_market_status_check = time.time()
    bot._market_close_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
    bot.inv_manager.get_balance = MagicMock(return_value=10000)
    bot.inv_manager.get_position = MagicMock(return_value=position)
    bot.ob_manager.get_best_bid = MagicMock(return_value=(49, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(51, 10))
    bot._update_quotes = AsyncMock()
    bot.liquidate_inventory = AsyncMock(return_value=False)
    await bot._tick()
    bot._update_quotes.assert_not_awaited()
    bot.liquidate_inventory.assert_awaited_once_with("OLD")


def test_ticker_position_check_paginates_and_keeps_shards_separate():
    bot = AvellanedaStoikovBot(ticker="OLD")
    pages = [
        {"market_positions": [{"ticker": "OLD", "exchange_index": 0, "position_fp": "1.25"}], "cursor": "next"},
        {"market_positions": [{"ticker": "OLD", "exchange_index": 3, "position_fp": "-1.25"}], "cursor": ""},
    ]
    requests = MagicMock(side_effect=[MagicMock(status_code=200, json=MagicMock(return_value=page)) for page in pages])
    with patch("data.inventory_manager.requests.get", requests):
        positions = bot.inv_manager._fetch_ticker_positions("OLD")
    assert positions == {0: Decimal("1.25"), 3: Decimal("-1.25")}
    assert requests.call_args_list[0].kwargs["params"]["ticker"] == "OLD"
    assert requests.call_args_list[1].kwargs["params"]["cursor"] == "next"
    assert "exchange_index" not in requests.call_args_list[0].kwargs["params"]


@pytest.mark.parametrize("page", [
    {"market_positions": [], "cursor": None},
    {"market_positions": [{"ticker": "OTHER", "exchange_index": 0, "position_fp": "0"}], "cursor": ""},
    {"market_positions": [{"ticker": "OLD", "position_fp": "0"}], "cursor": ""},
    {"market_positions": [{"ticker": "OLD", "exchange_index": 0, "position_fp": "NaN"}], "cursor": ""},
])
def test_ticker_position_check_rejects_malformed_pages(page):
    bot = AvellanedaStoikovBot(ticker="OLD")
    response = MagicMock(status_code=200, json=MagicMock(return_value=page))
    with patch("data.inventory_manager.requests.get", return_value=response):
        assert bot.inv_manager._fetch_ticker_positions("OLD") is None


def test_ticker_position_check_rejects_repeated_cursor():
    bot = AvellanedaStoikovBot(ticker="OLD")
    response = MagicMock(status_code=200, json=MagicMock(return_value={"market_positions": [], "cursor": "same"}))
    with patch("data.inventory_manager.requests.get", return_value=response):
        assert bot.inv_manager._fetch_ticker_positions("OLD") is None


@pytest.mark.asyncio
async def test_fill_during_position_fetch_invalidates_snapshot():
    bot = AvellanedaStoikovBot(ticker="OLD")

    def fetch(ticker):
        bot.inv_manager._fill_count += 1
        return {}

    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=fetch)
    assert await bot._verified_ticker_positions("OLD") is None


@pytest.mark.asyncio
async def test_pending_fill_during_position_fetch_invalidates_snapshot():
    bot = AvellanedaStoikovBot(ticker="OLD")

    def fetch(ticker):
        # A fill arrived and is buffering/spooling, incrementing _pending_fills before _fill_count
        bot.inv_manager._pending_fills += 1
        return {}

    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=fetch)
    assert await bot._verified_ticker_positions("OLD") is None


@pytest.mark.asyncio
async def test_preexisting_pending_fills_invalidates_snapshot():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot.inv_manager._pending_fills = 1
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})
    assert await bot._verified_ticker_positions("OLD") is None


@pytest.mark.asyncio
async def test_liquidate_inventory_rejects_flatness_if_fills_pending():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    # Positions on exchange appear empty, but an in-flight fill is pending in ingestion
    bot._verified_ticker_positions = AsyncMock(return_value={})
    bot.inv_manager._pending_fills = 1
    bot.inv_manager.get_position = MagicMock(return_value=0)

    assert await bot.liquidate_inventory("OLD") is False


@pytest.mark.asyncio
async def test_partial_fill_uses_exchange_residual_for_next_order():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[
        {0: Decimal("1.25")}, {0: Decimal("0.25")}, {0: Decimal("0.25")}, {},
    ])
    bot.om.place_order = AsyncMock(side_effect=["first", "second"])
    bot.om.get_order_status = AsyncMock(side_effect=[
        {"status": "canceled", "fill_count_fp": "1.00"},
        {"status": "executed", "fill_count_fp": "0.25"},
    ])
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))
    with patch("strategy.market_maker.asyncio.sleep", new=AsyncMock()):
        assert await bot.liquidate_inventory()
    assert [call.kwargs["count"] for call in bot.om.place_order.await_args_list] == [Decimal("1.25"), Decimal("0.25")]


@pytest.mark.asyncio
async def test_stale_position_snapshot_cannot_confirm_fill():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("0.25")})
    bot.om.place_order = AsyncMock(return_value="order")
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "0.25"})
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))
    with patch("strategy.market_maker.asyncio.sleep", new=AsyncMock()):
        assert not await bot.liquidate_inventory()
    bot.om.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_placement_still_rechecks_exchange():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: Decimal("0.02")}, {}])
    bot.om.place_order = AsyncMock(side_effect=RuntimeError("timeout"))
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))
    assert not await bot.liquidate_inventory()
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2


@pytest.mark.asyncio
async def test_rotation_reverts_subscription_if_fill_arrives_during_swap():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})

    async def unsubscribe(tickers):
        if tickers == ["OLD"]:
            bot.inv_manager._fill_count += 1

    bot.ob_manager.unsubscribe = AsyncMock(side_effect=unsubscribe)
    bot.ob_manager.subscribe = AsyncMock()
    with patch("strategy.market_maker.send_alert", new=AsyncMock()):
        assert not await bot.rotate_market("NEW")
    assert bot.ticker == "OLD"
    assert bot.ob_manager.subscribe.await_args_list[-1].args == (["OLD"],)


@pytest.mark.asyncio
async def test_remote_fill_without_websocket_delivery_rejects_disagreeing_liquidation_snapshot():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.place_order = AsyncMock(return_value="order")
    bot.om.get_order_status = AsyncMock(return_value={"status": "canceled", "fill_count_fp": "0.50"})
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[
        {0: Decimal("1.25")}, {0: Decimal("0.25")},
    ])
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))
    with patch("strategy.market_maker.asyncio.sleep", new=AsyncMock()):
        assert not await bot.liquidate_inventory()
    bot.om.place_order.assert_awaited_once()
    assert bot.inv_manager._fill_count == 0
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2


@pytest.mark.asyncio
async def test_remote_fill_without_websocket_delivery_during_swap_reverts_rotation():
    bot = AvellanedaStoikovBot(ticker="OLD")
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{}, {}, {0: Decimal("0.02")}])
    bot.ob_manager.unsubscribe = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()
    with patch("strategy.market_maker.send_alert", new=AsyncMock()):
        assert not await bot.rotate_market("NEW")
    assert bot.ticker == "OLD"
    assert bot.inv_manager._fill_count == 0
    assert bot.ob_manager.unsubscribe.await_args_list[-1].args == (["NEW"],)
    assert bot.ob_manager.subscribe.await_args_list[-1].args == (["OLD"],)


@pytest.mark.asyncio
async def test_stop_liquidates_exchange_fractional_when_local_is_flat():
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: Decimal("0.02")}, {0: Decimal("0.02")}, {}, {}, {}, {}])
    bot.om.place_order = AsyncMock(return_value="order")
    bot.om.get_order_status = AsyncMock(return_value={"status": "executed", "fill_count_fp": "0.02"})
    bot.om.reconcile_resting_orders = AsyncMock(return_value=0)
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.ob_manager.get_best_bid = MagicMock(return_value=(45, 10))
    bot.ob_manager.get_best_ask = MagicMock(return_value=(55, 10))

    assert await bot.stop() is True
    assert bot.om.place_order.await_args.kwargs == {
        "ticker": "OLD", "side": "yes", "action": "sell", "count": Decimal("0.02"),
        "price": 45, "reduce_only": True, "time_in_force": "immediate_or_cancel", "exchange_index": 0,
    }
    assert bot.inv_manager._fetch_ticker_positions.call_count >= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [None, {}])
async def test_stop_requires_confirmed_exchange_flatness(positions):
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value=positions)
    bot.liquidate_inventory = AsyncMock()
    bot.om.record_pnl_snapshot_async = AsyncMock()

    assert await bot.stop() is (positions is not None)
    assert bot.inv_manager._fetch_ticker_positions.call_count >= 1
    bot.liquidate_inventory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [None, {0: Decimal("0.02")}])
async def test_stop_rechecks_exchange_after_liquidation(after):
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: Decimal("0.02")}, after])
    bot.liquidate_inventory = AsyncMock(return_value=True)
    bot.om.record_pnl_snapshot_async = AsyncMock()

    assert await bot.stop() is False
    bot.liquidate_inventory.assert_awaited_once_with("OLD")
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2


@pytest.mark.asyncio
async def test_stop_rechecks_exchange_after_liquidation_exception():
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{0: Decimal("0.02")}, {}])
    bot.liquidate_inventory = AsyncMock(side_effect=RuntimeError("timeout"))
    bot.om.record_pnl_snapshot_async = AsyncMock()

    assert await bot.stop() is False
    assert bot.inv_manager._fetch_ticker_positions.call_count == 2
    bot.om.record_pnl_snapshot_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_fails_on_unresolved_local_disagreement_after_liquidation():
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager.get_position = MagicMock(return_value=0.02)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={})
    bot.liquidate_inventory = AsyncMock(return_value=True)
    bot.om.record_pnl_snapshot_async = AsyncMock()

    assert await bot.stop() is False
    bot.liquidate_inventory.assert_awaited_once_with("OLD")


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_only", [False, True])
async def test_stop_fails_on_late_fill_during_snapshot(remote_only):
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{}, {0: Decimal("0.02")} if remote_only else {}])

    async def snapshot(**kwargs):
        if not remote_only:
            bot.inv_manager._fill_count += 1

    bot.om.record_pnl_snapshot_async = AsyncMock(side_effect=snapshot)
    bot.auto_sweep_reverse_enabled = True
    bot.sweep_shard_to_shard0 = AsyncMock()

    assert await bot.stop() is False
    bot.sweep_shard_to_shard0.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_fails_when_fill_races_exchange_check():
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)

    def fetch(ticker):
        bot.inv_manager._fill_count += 1
        return {}

    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=fetch)
    bot.om.record_pnl_snapshot_async = AsyncMock()

    assert await bot.stop() is False


@pytest.mark.asyncio
async def test_stop_does_not_liquidate_without_confirmed_cancellation():
    bot = AvellanedaStoikovBot(ticker="OLD")
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=False)
    bot._escalate_to_kill_switch = AsyncMock(return_value=False)
    bot.inv_manager._fetch_ticker_positions = MagicMock(return_value={0: Decimal("0.02")})
    bot.liquidate_inventory = AsyncMock()
    bot.om.record_pnl_snapshot_async = AsyncMock()

    assert await bot.stop() is False
    bot.inv_manager._fetch_ticker_positions.assert_not_called()
    bot.liquidate_inventory.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("remote_only", [False, True])
async def test_stop_rechecks_after_collateral_sweep(remote_only):
    bot = AvellanedaStoikovBot(ticker="OLD", auto_sweep_reverse_enabled=True)
    setup_healthy_feed(bot)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager._fetch_ticker_positions = MagicMock(side_effect=[{}, {}, {0: Decimal("0.02")} if remote_only else {}])
    bot.om.record_pnl_snapshot_async = AsyncMock()

    async def sweep(**kwargs):
        if not remote_only:
            bot.inv_manager._fill_count += 1

    bot.sweep_shard_to_shard0 = AsyncMock(side_effect=sweep)
    assert await bot.stop() is False
    bot.sweep_shard_to_shard0.assert_awaited_once()