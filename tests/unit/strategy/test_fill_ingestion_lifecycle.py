import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.fill_store import FillStore
from data.websocket_client import FatalIngestionError
from strategy.market_maker import AvellanedaStoikovBot


@pytest.mark.asyncio
async def test_fatal_websocket_stops_strategy_withdraws_and_blocks_restart():
    bot = AvellanedaStoikovBot(ticker="TEST")
    entered = asyncio.Event()

    async def strategy():
        bot.running = True
        entered.set()
        await asyncio.Event().wait()

    async def websocket():
        await entered.wait()
        raise FatalIngestionError("disk full")

    bot._run_strategy = strategy
    bot.ws_client.connect = websocket
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    with pytest.raises(FatalIngestionError, match="disk full"):
        await bot.start()
    assert not bot.running
    bot._cancel_all_quotes.assert_awaited_once()
    with pytest.raises(FatalIngestionError, match="reconcile"):
        await bot.start()


@pytest.mark.asyncio
async def test_spool_startup_failure_withdraws_orders():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.ws_client.is_connected = True
    async def websocket():
        await asyncio.Event().wait()
    bot.ws_client.connect = websocket
    bot.om.sync_and_recover_state = AsyncMock()
    bot.inv_manager.hydrate = AsyncMock(return_value=True)
    bot.exchange_index = 0
    bot.ensure_shard_collateral = AsyncMock(return_value=True)
    bot._refresh_market_metadata = AsyncMock()
    bot.inv_manager.subscribe = AsyncMock(side_effect=OSError("spool unavailable"))
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.inv_manager.close_fill_ingestion = AsyncMock()
    with patch("strategy.market_maker.start_metrics_server"):
        with pytest.raises(FatalIngestionError, match="startup"):
            await bot.start()
    bot._cancel_all_quotes.assert_awaited_once()
    await bot.stop()
    bot.inv_manager.close_fill_ingestion.assert_awaited_once_with(clean=False)


@pytest.mark.asyncio
async def test_shutdown_stops_ws_before_ingestion_and_pool():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    order = []
    started = asyncio.Event()

    async def websocket():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("websocket")

    bot._ws_task = asyncio.create_task(websocket())
    await started.wait()
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._verified_ticker_positions = AsyncMock(return_value={})
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.inv_manager.close_fill_ingestion = AsyncMock(side_effect=lambda **kwargs: order.append("ingestion") or True)
    bot.om.close = MagicMock(side_effect=lambda: order.append("pool"))
    assert await bot.stop()
    assert order == ["websocket", "ingestion", "pool"]


@pytest.mark.asyncio
async def test_start_without_store_does_not_launch_fill_sync():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = None
    bot.ws_client.is_connected = True
    bot.ws_client.connect = asyncio.Event().wait
    bot.om.sync_and_recover_state = AsyncMock()
    bot.inv_manager.hydrate = AsyncMock(return_value=True)
    bot.exchange_index = 0
    bot.ensure_shard_collateral = AsyncMock(return_value=True)
    bot._refresh_market_metadata = AsyncMock()
    bot.inv_manager.subscribe = AsyncMock()
    bot._subscribe_fills_before_hydration = AsyncMock()
    bot.ob_manager.subscribe = AsyncMock()
    bot._tick = AsyncMock(side_effect=RuntimeError("tick stopped"))
    with patch("strategy.market_maker.start_metrics_server"), patch("strategy.market_maker.FillSync") as sync, patch("strategy.market_maker.send_alert", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="tick stopped"):
            await bot.start()
    sync.assert_not_called()
    bot._ws_task.cancel()
    for task in list(bot._background_tasks):
        task.cancel()
    await asyncio.gather(bot._ws_task, *bot._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_stop_waits_for_strategy_cancellation_before_withdrawal():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    entered = asyncio.Event()
    order = []

    async def strategy():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            order.append("strategy stopped")

    bot._run_strategy = strategy
    bot.ws_client.connect = asyncio.Event().wait
    bot._cancel_all_quotes = AsyncMock(side_effect=lambda: order.append("withdraw") or True)
    bot._verified_ticker_positions = AsyncMock(return_value={})
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.inv_manager.close_fill_ingestion = AsyncMock()
    bot.om.close = MagicMock()
    start_task = asyncio.create_task(bot.start())
    await entered.wait()
    assert await bot.stop()
    await asyncio.gather(start_task, return_exceptions=True)
    assert order == ["strategy stopped", "withdraw"]


@pytest.mark.asyncio
async def test_subscription_acknowledged_before_hydration_and_quotes():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = None
    bot.ws_client.is_connected = True
    bot.ws_client.connect = asyncio.Event().wait
    bot.om.sync_and_recover_state = AsyncMock()
    bot.exchange_index = 0
    bot.ensure_shard_collateral = AsyncMock(return_value=True)
    bot._refresh_market_metadata = AsyncMock()
    order = []

    async def send(message):
        order.append("subscribe")
        for handler in list(bot.ws_client.message_handlers):
            await handler({"type": "subscribed", "id": message["id"], "msg": {"channel": "fill"}})

    async def hydrate(**kwargs):
        order.append("hydrate")
        return True

    bot.ws_client.send_message = send
    bot.inv_manager.hydrate = hydrate
    bot.ob_manager.subscribe = AsyncMock()
    bot._tick = AsyncMock(side_effect=RuntimeError("tick stopped"))
    with patch("strategy.market_maker.start_metrics_server"), patch("strategy.market_maker.send_alert", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="tick stopped"):
            await bot.start()
    assert order == ["subscribe", "hydrate"]
    bot._tick.assert_awaited_once()
    bot._ws_task.cancel()
    for task in list(bot._background_tasks):
        task.cancel()
    await asyncio.gather(bot._ws_task, *bot._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_fill_during_startup_snapshot_blocks_quotes():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.ws_client.is_connected = True
    bot.ws_client.connect = asyncio.Event().wait
    bot.om.sync_and_recover_state = AsyncMock()
    bot.exchange_index = 0
    bot.ensure_shard_collateral = AsyncMock(return_value=True)
    bot._refresh_market_metadata = AsyncMock()
    bot._subscribe_fills_before_hydration = AsyncMock()
    async def hydrate(**kwargs):
        bot.inv_manager._fill_count += 1
        return False
    bot.inv_manager.hydrate = hydrate
    bot._tick = AsyncMock()
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    with patch("strategy.market_maker.start_metrics_server"):
        with pytest.raises(FatalIngestionError, match="raced"):
            await bot.start()
    bot._tick.assert_not_awaited()
    bot._cancel_all_quotes.assert_awaited_once()
    bot._ws_task.cancel()
    await asyncio.gather(bot._ws_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_rejected_fill_subscription_fails_closed():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = None
    bot.ws_client.is_connected = True

    async def send(message):
        for handler in list(bot.ws_client.message_handlers):
            await handler({"type": "error", "id": message["id"]})

    bot.ws_client.send_message = send
    with pytest.raises(FatalIngestionError, match="startup"):
        await bot._subscribe_fills_before_hydration()


@pytest.mark.asyncio
async def test_connection_generation_change_stops_quoting():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = None
    bot.ws_client.is_connected = True
    bot.ws_client.connect = asyncio.Event().wait
    bot.om.sync_and_recover_state = AsyncMock()
    bot.exchange_index = 0
    bot.ensure_shard_collateral = AsyncMock(return_value=True)
    bot._refresh_market_metadata = AsyncMock()
    bot._subscribe_fills_before_hydration = AsyncMock()
    bot.inv_manager.hydrate = AsyncMock(return_value=True)
    bot.inv_manager._sync_loop = asyncio.Event().wait
    bot.ob_manager.subscribe = AsyncMock()
    async def tick():
        bot.ws_client.connection_generation += 1
    bot._tick = AsyncMock(side_effect=tick)
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    with patch("strategy.market_maker.start_metrics_server"), patch("strategy.market_maker.send_alert", new_callable=AsyncMock), patch("strategy.market_maker.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(FatalIngestionError, match="connection changed"):
            await bot.start()
    bot._tick.assert_awaited_once()
    bot._cancel_all_quotes.assert_awaited_once()
    bot._ws_task.cancel()
    for task in list(bot._background_tasks):
        task.cancel()
    await asyncio.gather(bot._ws_task, *bot._background_tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "final_check", "reconnect", "prior_failure", "cancel"])
async def test_shutdown_latch_requires_complete_healthy_verification(failure):
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    bot._ws_task = asyncio.create_task(asyncio.Event().wait())
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.om.close = MagicMock()
    bot.inv_manager.close_fill_ingestion = AsyncMock()
    checks = 0

    async def verify(ticker):
        nonlocal checks
        checks += 1
        if failure == "reconnect" and checks == 1:
            bot.ws_client.connection_generation += 1
        if failure == "cancel" and checks == 1:
            raise asyncio.CancelledError()
        if failure == "final_check" and checks == 3:
            return None
        return {}

    bot._verified_ticker_positions = verify
    bot._ingestion_failed = failure == "prior_failure"
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await bot.stop()
    else:
        assert await bot.stop() is (failure == "none")
    bot.inv_manager.close_fill_ingestion.assert_awaited_once_with(clean=failure == "none")
    assert bot._ws_task.done()
    bot.om.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_start_withdraws_orders_and_latches_shutdown():
    bot = AvellanedaStoikovBot(ticker="TEST")
    entered = asyncio.Event()

    async def strategy():
        entered.set()
        await asyncio.Event().wait()

    bot._run_strategy = strategy
    bot.ws_client.connect = asyncio.Event().wait
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.inv_manager.close_fill_ingestion = AsyncMock()
    bot.om.close = MagicMock()
    task = asyncio.create_task(bot.start())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bot._cancel_all_quotes.assert_awaited_once()
    assert not await bot.stop()
    bot.inv_manager.close_fill_ingestion.assert_awaited_once_with(clean=False)


def test_default_constructor_wires_fill_store():
    bot = AvellanedaStoikovBot(ticker="TEST")
    assert isinstance(bot.fill_store, FillStore)
    assert bot.inv_manager.fill_store is bot.fill_store


@pytest.mark.asyncio
async def test_signal_shutdown_with_request_stop_is_clean():
    bot = AvellanedaStoikovBot(ticker="TEST")
    entered = asyncio.Event()

    async def strategy():
        entered.set()
        await asyncio.Event().wait()

    bot._run_strategy = strategy
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    bot.ws_client.connect = asyncio.Event().wait
    bot.om.sync_and_recover_state = AsyncMock()
    bot.exchange_index = 0
    bot.ensure_shard_collateral = AsyncMock(return_value=True)
    bot._refresh_market_metadata = AsyncMock()
    bot.inv_manager.subscribe = AsyncMock()
    bot._subscribe_fills_before_hydration = AsyncMock()
    bot.inv_manager.hydrate = AsyncMock(return_value=True)
    bot.ob_manager.subscribe = AsyncMock()
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._verified_ticker_positions = AsyncMock(return_value={"TEST": 0})
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.inv_manager.close_fill_ingestion = AsyncMock(return_value=True)
    bot.om.close = MagicMock()

    task = asyncio.create_task(bot.start())
    await entered.wait()

    # Coordinated signal shutdown
    bot.request_stop()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert bot._ingestion_failed is False
    assert await bot.stop()
    bot.inv_manager.close_fill_ingestion.assert_awaited_with(clean=True)
    bot.inv_manager.close_fill_ingestion.assert_awaited_with(clean=True)


@pytest.mark.asyncio
async def test_ingestion_failure_blocks_local_inventory_liquidation():
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot._ingestion_failed = True
    bot.inv_manager.positions = {"TEST": 5}
    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.liquidate_inventory = AsyncMock()
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.inv_manager.close_fill_ingestion = AsyncMock(return_value=False)
    bot.om.close = MagicMock()

    res = await bot.stop()
    assert res is False
    bot.liquidate_inventory.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_drain_timeout_defers_cleanup_until_blocked_record_finishes(tmp_path, monkeypatch):
    import threading

    spool_path = str(tmp_path / "spool.sqlite3")
    monkeypatch.setenv("FILL_SPOOL_PATH", spool_path)

    bot = AvellanedaStoikovBot(ticker="TEST")
    bot._fill_drain_timeout = 0.05
    bot._fill_cancel_grace_timeout = 0.5
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    bot.ws_client.subscribe = AsyncMock()

    # Provide an active _ws_task so feed_healthy() is True and shutdown begins clean
    ws_started = asyncio.Event()
    async def websocket_loop():
        ws_started.set()
        await asyncio.Event().wait()

    bot._ws_task = asyncio.create_task(websocket_loop())
    await ws_started.wait()

    record_entered = threading.Event()
    record_release = threading.Event()

    mock_store = MagicMock()
    mock_store.initialize.return_value = True

    def blocking_record(fill, source):
        record_entered.set()
        record_release.wait(timeout=3.0)
        return False

    mock_store.record.side_effect = blocking_record
    # When cancel_in_flight is triggered, unblock the record thread cooperatively (returning False on cancellation)
    mock_store.cancel_in_flight = MagicMock(side_effect=lambda force=False: record_release.set())
    bot.fill_store = mock_store
    bot.inv_manager.fill_store = mock_store

    await bot.inv_manager.subscribe()

    await bot.inv_manager._handle_message({
        "type": "fill",
        "msg": {"trade_id": "t1", "action": "buy", "side": "yes", "count": 1, "yes_price": 50, "market_ticker": "TEST"}
    })

    await asyncio.to_thread(record_entered.wait, 2.0)
    assert record_entered.is_set()

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._verified_ticker_positions = AsyncMock(return_value={})
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.om.close = MagicMock()

    # Preconditions: feed is healthy, ingestion has not failed, ws task running
    assert bot._ingestion_failed is False
    assert not bot._ws_task.done()

    # stop() starts clean, times out on drain, invokes cancel_in_flight, coordinates cleanup before return
    clean = await bot.stop()

    # 1. stop() must return False specifically due to undrained worker
    assert clean is False
    assert bot._ingestion_failed is True

    # 2. cancel_in_flight was invoked to interrupt the blocked database thread
    mock_store.cancel_in_flight.assert_called_with(force=False)
    assert record_release.is_set()

    # 3. initialize was NOT called again during shutdown despite record() returning False
    assert mock_store.initialize.call_count == 1

    # 4. Spool and pool are cleanly closed before stop() returns, and worker thread completed
    bot.om.close.assert_called_once()
    assert bot.inv_manager._fill_spool is None
    assert bot.inv_manager._fill_worker.done()
    assert bot._ws_task.done()


@pytest.mark.asyncio
async def test_shutdown_forced_socket_closure_when_cancellation_unresponsive(tmp_path, monkeypatch):
    import threading

    spool_path = str(tmp_path / "spool.sqlite3")
    monkeypatch.setenv("FILL_SPOOL_PATH", spool_path)

    bot = AvellanedaStoikovBot(ticker="TEST")
    bot._fill_drain_timeout = 0.05
    bot._fill_cancel_grace_timeout = 0.05
    bot.ws_client.is_connected = True
    bot._fill_connection_generation = bot.ws_client.connection_generation
    bot.ws_client.subscribe = AsyncMock()

    ws_started = asyncio.Event()
    async def websocket_loop():
        ws_started.set()
        await asyncio.Event().wait()

    bot._ws_task = asyncio.create_task(websocket_loop())
    await ws_started.wait()

    record_entered = threading.Event()
    record_release = threading.Event()

    mock_store = MagicMock()
    mock_store.initialize.return_value = True

    def blocking_record(fill, source):
        record_entered.set()
        record_release.wait(timeout=3.0)
        return False

    mock_store.record.side_effect = blocking_record

    # Simulate unresponsive database: cancel_in_flight(force=False) is ignored,
    # but cancel_in_flight(force=True) forcefully tears down the socket to unblock the thread.
    def cancel_handler(force=False):
        if force:
            record_release.set()

    mock_store.cancel_in_flight = MagicMock(side_effect=cancel_handler)
    bot.fill_store = mock_store
    bot.inv_manager.fill_store = mock_store

    await bot.inv_manager.subscribe()

    await bot.inv_manager._handle_message({
        "type": "fill",
        "msg": {"trade_id": "t1", "action": "buy", "side": "yes", "count": 1, "yes_price": 50, "market_ticker": "TEST"}
    })

    await asyncio.to_thread(record_entered.wait, 2.0)
    assert record_entered.is_set()

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot._verified_ticker_positions = AsyncMock(return_value={})
    bot.om.record_pnl_snapshot_async = AsyncMock()
    bot.om.close = MagicMock()

    # Execute stop(): cooperative cancellation will be ignored, triggering forced socket closure
    clean = await bot.stop()

    assert clean is False
    assert bot._ingestion_failed is True

    # Both cooperative and forced cancellation were invoked
    assert mock_store.cancel_in_flight.call_count == 2
    mock_store.cancel_in_flight.assert_any_call(force=False)
    mock_store.cancel_in_flight.assert_any_call(force=True)
    assert record_release.is_set()

    # initialize was NOT called during shutdown
    assert mock_store.initialize.call_count == 1

    # Cleanup completed deterministically before stop() returned
    bot.om.close.assert_called_once()
    assert bot.inv_manager._fill_spool is None
    assert bot.inv_manager._fill_worker.done()
    assert bot._ws_task.done()


