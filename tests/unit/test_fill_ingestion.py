import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.inventory_manager import InventoryManager
from data.websocket_client import FatalIngestionError


def valid_fill(trade_id):
    return {"trade_id": trade_id, "market_ticker": "T", "side": "yes", "action": "buy", "count": 1, "price": 50}


@pytest.fixture(autouse=True)
def spool_path(tmp_path, monkeypatch):
    monkeypatch.setenv("FILL_SPOOL_PATH", str(tmp_path / "spool.db"))


async def start(manager):
    manager.ws_client.subscribe = AsyncMock()
    await manager.subscribe()


async def drain(manager):
    async def wait():
        while await manager._spool_call(manager._fill_spool.pending) is not None:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 2)
    await manager.close_fill_ingestion()


@pytest.mark.asyncio
async def test_failed_write_retries_original_payload_without_blocking_inventory():
    store = MagicMock()
    store.record.side_effect = [False, True]
    manager = InventoryManager(MagicMock(), fill_store=store)
    manager._fill_retry_interval = 0.001
    await start(manager)
    fill = {"trade_id": "t", "market_ticker": "T", "side": "yes", "action": "buy",
            "count": 1, "price": 50, "websocket_only": "retained"}
    await manager._handle_message({"type": "fill", "msg": fill})
    assert manager.get_position("T") == 1
    await drain(manager)
    assert store.record.call_count == 2
    assert store.record.call_args_list[1].args == (fill, "websocket")


@pytest.mark.asyncio
async def test_rejected_fill_rotates_behind_waiting_fills_and_recovers():
    started = threading.Event()
    release = threading.Event()
    attempts = []
    store = MagicMock()

    def record(fill, source):
        attempts.append(fill["trade_id"])
        if len(attempts) == 1:
            started.set()
            release.wait(2)
            return False
        return True

    store.record.side_effect = record
    manager = InventoryManager(MagicMock(), fill_store=store)
    manager._fill_retry_interval = 0.001
    await start(manager)
    first = dict(valid_fill("first"), websocket_only="retained")
    second = valid_fill("second")
    try:
        await manager._handle_message({"type": "fill", "msg": first})
        assert await asyncio.to_thread(started.wait, 1)
        await manager._handle_message({"type": "fill", "msg": second})
    finally:
        release.set()
    await drain(manager)
    assert attempts == ["first", "second", "first"]
    assert store.record.call_args_list[-1].args == (first, "websocket")


@pytest.mark.asyncio
async def test_permanent_failure_does_not_prevent_later_write(caplog):
    store = MagicMock()
    store.record.side_effect = lambda fill, source: fill["trade_id"] != "bad"
    manager = InventoryManager(MagicMock(), fill_store=store)
    manager._fill_retry_interval = 0.001
    await start(manager)
    try:
        await manager._handle_message({"type": "fill", "msg": valid_fill("bad")})
        await manager._handle_message({"type": "fill", "msg": valid_fill("good")})
        for _ in range(100):
            if any(call.args[0]["trade_id"] == "good" for call in store.record.call_args_list):
                break
            await asyncio.sleep(0.001)
        assert any(call.args[0]["trade_id"] == "good" for call in store.record.call_args_list)
        assert "WebSocket fill persistence failed" in caplog.text
    finally:
        await manager.close_fill_ingestion()


@pytest.mark.asyncio
async def test_initialize_before_subscribe():
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    store = MagicMock()
    store.initialize.return_value = True
    manager = InventoryManager(ws, fill_store=store)
    async def subscribe(*args):
        assert manager._fill_init_task is not None
    ws.subscribe.side_effect = subscribe
    await manager.subscribe()
    await manager._fill_init_task
    store.initialize.assert_called_once()
    await manager.close_fill_ingestion()


@pytest.mark.asyncio
async def test_slow_initialization_does_not_delay_subscription_or_inventory():
    started = threading.Event()
    release = threading.Event()
    store = MagicMock()
    def initialize():
        started.set()
        release.wait(2)
        return True
    store.initialize.side_effect = initialize
    store.record.return_value = True
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    manager = InventoryManager(ws, fill_store=store)
    try:
        await manager.subscribe()
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        await manager._handle_message({"type": "fill", "msg": {"trade_id": "t", "market_ticker": "T",
                                      "side": "yes", "action": "buy", "count": 1, "price": 50}})
        assert manager.get_position("T") == 1
        store.record.assert_not_called()
    finally:
        release.set()
        await drain(manager)
    store.record.assert_called_once()


@pytest.mark.asyncio
async def test_websocket_fill_recorded_off_thread_after_inventory():
    ws = MagicMock()
    store = MagicMock()
    manager = InventoryManager(ws, fill_store=store)
    await start(manager)
    loop_thread = threading.get_ident()
    fill = {"trade_id": "t", "order_id": "o", "market_ticker": "T", "side": "no", "action": "buy",
            "count_fp": "0.25", "yes_price_dollars": "0.7500", "fee_cost": "0.001"}
    def record(raw, source):
        assert threading.get_ident() != loop_thread
        assert manager.get_position("T") == -0.25
        assert raw == fill
        assert source == "websocket"
        return True
    store.record.side_effect = record
    await ws.add_message_handler.call_args.args[0]({"type": "fill", "msg": fill})
    await drain(manager)
    store.record.assert_called_once_with(fill, "websocket")
    assert manager._fill_count == 1


@pytest.mark.asyncio
async def test_database_error_does_not_interrupt_inventory(caplog):
    store = MagicMock()
    store.record.side_effect = RuntimeError("unavailable")
    manager = InventoryManager(MagicMock(), fill_store=store)
    await start(manager)
    await manager._handle_message({"type": "fill", "msg": valid_fill("database-error")})
    for _ in range(100):
        if store.record.called and "WebSocket fill persistence failed" in caplog.text:
            break
        await asyncio.sleep(0.001)
    assert manager.get_position("T") == 1
    assert "WebSocket fill persistence failed" in caplog.text
    await manager.close_fill_ingestion()


@pytest.mark.asyncio
async def test_no_store_and_partial_fill_still_persisted():
    manager = InventoryManager(MagicMock())
    await manager._handle_message({"type": "fill", "msg": valid_fill("no-store")})
    assert manager.get_position("T") == 1
    store = MagicMock()
    manager.fill_store = store
    await start(manager)
    partial = {"trade_id": "t", "order_id": "o"}
    with pytest.raises(FatalIngestionError):
        await manager._handle_message({"type": "fill", "msg": partial})
    await drain(manager)
    store.record.assert_called_once_with(partial, "websocket")
    assert manager._fill_count == 1


@pytest.mark.asyncio
async def test_fill_is_persisted_even_if_inventory_processing_raises():
    store = MagicMock()
    manager = InventoryManager(MagicMock(), fill_store=store)
    await start(manager)
    manager._handle_fill = MagicMock(side_effect=RuntimeError("inventory error"))
    fill = {"trade_id": "t", "order_id": "o"}
    with pytest.raises(FatalIngestionError, match="failed to process inventory fill") as error:
        await manager._handle_message({"type": "fill", "msg": fill})
    assert str(error.value.__cause__) == "inventory error"
    await drain(manager)
    store.record.assert_called_once_with(fill, "websocket")


@pytest.mark.asyncio
async def test_slow_database_does_not_block_event_loop():
    started = threading.Event()
    release = threading.Event()
    store = MagicMock()
    def record(*args):
        started.set()
        release.wait(2)
        return True
    store.record.side_effect = record
    manager = InventoryManager(MagicMock(), fill_store=store)
    await start(manager)
    task = asyncio.create_task(manager._handle_message({"type": "fill", "msg": valid_fill("t")}))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        assert task.done()
        assert not manager._fill_worker.done()
    finally:
        release.set()
        await task
        await drain(manager)


@pytest.mark.asyncio
async def test_spool_retains_fills_while_database_is_blocked():
    started = threading.Event()
    release = threading.Event()
    store = MagicMock()
    def record(*args):
        started.set()
        release.wait(2)
        return True
    store.record.side_effect = record
    manager = InventoryManager(MagicMock(), fill_store=store)
    await start(manager)
    fill = {"trade_id": "t", "market_ticker": "T", "side": "yes", "action": "buy", "count": 1, "price": 50}
    try:
        await manager._handle_message({"type": "fill", "msg": fill})
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        await manager._handle_message({"type": "fill", "msg": dict(fill, trade_id="t2")})
        await manager._handle_message({"type": "fill", "msg": dict(fill, trade_id="t3")})
        assert manager.get_position("T") == 3
        count = await manager._spool_call(lambda: manager._fill_spool._db.execute("SELECT count(*) FROM fills").fetchone()[0])
        assert count == 3
    finally:
        release.set()
        await drain(manager)
    assert store.record.call_count == 3