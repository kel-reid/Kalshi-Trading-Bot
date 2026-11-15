import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from data.fill_spool import FillSpool
from data.inventory_manager import InventoryManager
from data.websocket_client import FatalIngestionError
from data.websocket_client import KalshiWebsocketClient


def test_docker_spool_configuration():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text()
    ignore = (root / ".dockerignore").read_text()
    compose = (root / "docker-compose.yml").read_text()
    assert "mkdir -p /var/lib/kalshi-fill-spool && chown trader:trader /var/lib/kalshi-fill-spool" in dockerfile
    assert dockerfile.index("chown trader:trader /var/lib/kalshi-fill-spool") < dockerfile.index("USER trader")
    assert "data/*.sqlite*" in ignore and "data/*.db*" in ignore
    assert "fill_spool:/var/lib/kalshi-fill-spool" in compose


@pytest.mark.asyncio
async def test_unexpected_fill_handler_exception_is_fatal(monkeypatch):
    import data.websocket_client as module

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return '{"type":"fill","msg":{}}'

    connect = MagicMock(return_value=Socket())
    monkeypatch.setattr(module.websockets, "connect", connect)
    monkeypatch.setattr(module, "get_auth_headers", lambda *args: {})
    client = KalshiWebsocketClient()
    async def fail(message):
        raise ValueError("bad handler")
    client.add_message_handler(fail)
    with pytest.raises(FatalIngestionError) as error:
        await client.connect()
    assert isinstance(error.value.__cause__, ValueError)
    assert client._ingestion_failure is error.value
    connect.assert_called_once()


@pytest.mark.asyncio
async def test_duplicate_fill_updates_inventory_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FILL_SPOOL_PATH", str(tmp_path / "spool.db"))
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    manager = InventoryManager(ws, fill_store=MagicMock())
    await manager.subscribe()
    fill = {"trade_id": "same", "market_ticker": "T", "side": "yes", "action": "buy", "count": 1, "price": 50}
    try:
        await manager._handle_message({"type": "fill", "msg": fill})
        await manager._handle_message({"type": "fill", "msg": fill})
        assert manager.get_position("T") == 1
        assert manager.balance_cents == -50
        assert manager._fill_count == 1
    finally:
        await manager.close_fill_ingestion()


@pytest.mark.asyncio
async def test_identity_namespaces_and_explicit_alias():
    manager = InventoryManager(MagicMock())
    fill = {"trade_id": "same", "market_ticker": "T", "side": "yes", "action": "buy", "count": 1, "price": 50}
    await manager._handle_message({"type": "fill", "msg": fill})
    await manager._handle_message({"type": "fill", "msg": {**fill, "fill_id": "different"}})
    await manager._handle_message({"type": "fill", "msg": fill})
    assert manager.get_position("T") == 1
    await manager._handle_message({"type": "fill", "msg": {**fill, "trade_id": "second", "fill_id": "same"}})
    assert manager.get_position("T") == 2
    with pytest.raises(FatalIngestionError):
        await manager._handle_message({"type": "fill", "msg": {**fill, "trade_id": None, "fill_id": "different"}})


@pytest.mark.asyncio
async def test_cancelled_append_commit_retains_dirty_latch(tmp_path, monkeypatch):
    path = tmp_path / "spool.db"
    monkeypatch.setenv("FILL_SPOOL_PATH", str(path))
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    store = MagicMock()
    store.record.return_value = False
    manager = InventoryManager(ws, fill_store=store)
    await manager.subscribe()
    started, release = threading.Event(), threading.Event()
    append = manager._fill_spool.append
    def blocked_append(fill):
        started.set()
        assert release.wait(3)
        append(fill)
    manager._fill_spool.append = blocked_append
    fill = {"trade_id": "cancelled", "market_ticker": "T", "side": "yes", "action": "buy", "count": 1, "price": 50}
    task = asyncio.create_task(manager._handle_message({"type": "fill", "msg": fill}))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.get_position("T") == 0
    await manager.close_fill_ingestion(clean=True)
    spool = FillSpool(path)
    assert spool.dirty()
    assert spool.pending()[1] == fill
    spool.close()
    restarted = InventoryManager(ws, fill_store=store)
    with pytest.raises(FatalIngestionError):
        await restarted.subscribe()


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [
    {"trade_id": "id", "count": 2},
    {"trade_id": "id", "fill_id": "other"},
])
async def test_conflicting_fill_identity_fails_closed(tmp_path, monkeypatch, second):
    monkeypatch.setenv("FILL_SPOOL_PATH", str(tmp_path / "spool.db"))
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    manager = InventoryManager(ws, fill_store=MagicMock())
    await manager.subscribe()
    fill = {"trade_id": "id", "fill_id": "original", "market_ticker": "T", "side": "yes", "action": "buy", "count": 1, "price": 50}
    await manager._handle_message({"type": "fill", "msg": fill})
    with pytest.raises(FatalIngestionError):
        await manager._handle_message({"type": "fill", "msg": {**fill, **second}})
    assert manager.get_position("T") == 1
    await manager.close_fill_ingestion(clean=True)
    spool = FillSpool(tmp_path / "spool.db")
    assert spool.dirty()
    spool.close()


@pytest.mark.asyncio
async def test_missing_identity_and_default_close_retain_latch(tmp_path, monkeypatch):
    path = tmp_path / "spool.db"
    monkeypatch.setenv("FILL_SPOOL_PATH", str(path))
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    manager = InventoryManager(ws, fill_store=MagicMock())
    await manager.subscribe()
    await manager.close_fill_ingestion()
    spool = FillSpool(path)
    assert spool.dirty()
    spool.clear_dirty()
    spool.close()
    manager = InventoryManager(ws, fill_store=MagicMock())
    await manager.subscribe()
    with pytest.raises(FatalIngestionError):
        await manager._handle_message({"type": "fill", "msg": {"market_ticker": "T", "side": "yes", "action": "buy", "count": 1, "price": 50}})
    assert manager.get_position("T") == 0
    await manager.close_fill_ingestion(clean=True)
    spool = FillSpool(path)
    assert spool.dirty()
    spool.close()


@pytest.mark.asyncio
async def test_invalid_fill_fails_closed_and_latches_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("FILL_SPOOL_PATH", str(tmp_path / "spool.db"))
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    manager = InventoryManager(ws, fill_store=MagicMock())
    await manager.subscribe()
    with pytest.raises(FatalIngestionError):
        await manager._handle_message({"type": "fill", "msg": {"trade_id": "bad"}})
    await manager.close_fill_ingestion()
    restarted = InventoryManager(ws, fill_store=MagicMock())
    with pytest.raises(FatalIngestionError):
        await restarted.subscribe()
    await restarted.close_fill_ingestion()


def test_recovery_and_exclusive_owner(tmp_path):
    path = tmp_path / "spool.db"
    spool = FillSpool(path)
    payload = {"trade_id": "1", "nested": {"extra": [1, "value"]}}
    spool.append(payload)
    with pytest.raises(BlockingIOError):
        FillSpool(path)
    spool.close()
    spool = FillSpool(path)
    row_id, recovered = spool.pending()
    assert recovered == payload
    spool.acknowledge(row_id)
    assert spool.pending() is None
    spool.close()


@pytest.mark.asyncio
async def test_outage_recovery_and_failure_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("FILL_SPOOL_PATH", str(tmp_path / "spool.db"))
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    ws.fail_ingestion = AsyncMock()
    store = MagicMock()
    store.record.return_value = False
    manager = InventoryManager(ws, fill_store=store)
    manager._fill_retry_interval = 0.01
    await manager.subscribe()
    for n in range(1100):
        await manager._handle_message({"type": "fill", "msg": {"trade_id": str(n), "market_ticker": "T", "action": "buy", "side": "yes", "count": 1, "price": 50}})
    await manager.close_fill_ingestion(clean=True)
    spool = FillSpool(tmp_path / "spool.db")
    assert spool._db.execute("SELECT count(*) FROM fills").fetchone()[0] == 1100
    spool.close()
    store.record.return_value = True
    manager = InventoryManager(ws, fill_store=store)
    manager._fill_retry_interval = 0.001
    await manager.subscribe()
    for _ in range(1000):
        count = await manager._spool_call(lambda: manager._fill_spool._db.execute("SELECT count(*) FROM fills").fetchone()[0])
        if count == 0:
            break
        await asyncio.sleep(0.01)
    assert count == 0
    manager._fill_spool.append = MagicMock(side_effect=OSError("disk full"))
    with pytest.raises(FatalIngestionError):
        await manager._handle_message({"type": "fill", "msg": {"trade_id": "lost"}})
    with pytest.raises(FatalIngestionError):
        await manager._handle_message({"type": "fill", "msg": {"trade_id": "later"}})
    await manager.close_fill_ingestion()


@pytest.mark.asyncio
async def test_fatal_fill_handler_ends_connection_without_reconnect(monkeypatch):
    import data.websocket_client as module

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return '{"type":"fill","msg":{}}'

    connect = MagicMock(return_value=Socket())
    monkeypatch.setattr(module.websockets, "connect", connect)
    monkeypatch.setattr(module, "get_auth_headers", lambda *args: {})
    client = KalshiWebsocketClient()
    async def fail(message):
        raise FatalIngestionError("disk full")
    client.add_message_handler(fail)
    with pytest.raises(FatalIngestionError):
        await client.connect()
    assert not client.is_connected
    assert client.ws_connection is None
    connect.assert_called_once()


@pytest.mark.asyncio
async def test_crash_after_database_commit_replays_idempotently(tmp_path, monkeypatch):
    spool_path = tmp_path / "spool.db"
    database_path = tmp_path / "database.db"
    payload = {"trade_id": "crash-fill", "market_ticker": "T", "action": "buy", "side": "yes", "count": 1, "price": 50, "websocket_only": {"nested": [1, 2]}}
    script = '''
import asyncio, json, os, sqlite3, sys
from unittest.mock import AsyncMock, MagicMock
from data.inventory_manager import InventoryManager

class Store:
    def initialize(self):
        with sqlite3.connect(sys.argv[2]) as db:
            db.execute("CREATE TABLE IF NOT EXISTS fills (trade_id TEXT PRIMARY KEY, payload TEXT)")
        return True
    def record(self, fill, source):
        with sqlite3.connect(sys.argv[2]) as db:
            db.execute("INSERT OR IGNORE INTO fills VALUES (?, ?)", (fill["trade_id"], json.dumps(fill)))
        os._exit(73)

async def run():
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    manager = InventoryManager(ws, fill_store=Store())
    await manager.subscribe()
    await manager._handle_message({"type": "fill", "msg": json.loads(sys.argv[3])})
    await asyncio.sleep(10)

asyncio.run(run())
'''
    env = dict(os.environ, FILL_SPOOL_PATH=str(spool_path))
    result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script, str(spool_path), str(database_path), json.dumps(payload)], env=env, timeout=15)
    assert result.returncode == 73
    with sqlite3.connect(database_path) as db:
        assert db.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
    spool = FillSpool(spool_path)
    assert spool.pending()[1] == payload
    assert spool.dirty()
    spool.clear_dirty()  # Operator reconciliation, performed offline for this test.
    spool.close()

    class Store:
        attempts = 0

        def initialize(self):
            return True

        def record(self, fill, source):
            assert source == "websocket"
            self.attempts += 1
            with sqlite3.connect(database_path) as db:
                db.execute("INSERT OR IGNORE INTO fills VALUES (?, ?)", (fill["trade_id"], json.dumps(fill)))
            return True

    monkeypatch.setenv("FILL_SPOOL_PATH", str(spool_path))
    ws = MagicMock()
    ws.subscribe = AsyncMock()
    store = Store()
    manager = InventoryManager(ws, fill_store=store)
    await manager.subscribe()
    try:
        async def wait_for_replay():
            while await manager._spool_call(manager._fill_spool.pending) is not None:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_replay(), 3)
        assert store.attempts == 1
    finally:
        await manager.close_fill_ingestion()
    with sqlite3.connect(database_path) as db:
        rows = db.execute("SELECT trade_id, payload FROM fills").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == payload["trade_id"]
    assert json.loads(rows[0][1]) == payload


@pytest.mark.asyncio
async def test_disk_failure_from_manager_stops_websocket(tmp_path, monkeypatch):
    import data.websocket_client as module

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, message):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return json.dumps({"type": "fill", "msg": {"trade_id": "lost"}})

    connect = MagicMock(return_value=Socket())
    monkeypatch.setattr(module.websockets, "connect", connect)
    monkeypatch.setattr(module, "get_auth_headers", lambda *args: {})
    monkeypatch.setenv("FILL_SPOOL_PATH", str(tmp_path / "spool.db"))
    client = KalshiWebsocketClient()
    manager = InventoryManager(client, fill_store=MagicMock())
    await manager.subscribe()
    manager._fill_spool.append = MagicMock(side_effect=OSError("disk full"))
    try:
        with pytest.raises(FatalIngestionError, match="failed to durably spool fill") as error:
            await client.connect()
        assert isinstance(error.value.__cause__, OSError)
        assert not client.is_connected
        assert client.ws_connection is None
        connect.assert_called_once()
    finally:
        await manager.close_fill_ingestion()