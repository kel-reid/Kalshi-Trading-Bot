import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from data.fill_sync import FillSync


@pytest.mark.asyncio
async def test_startup_and_reconnect_reconcile_without_overlap():
    ws = SimpleNamespace(is_connected=True, connection_generation=1)
    store = MagicMock()
    store.initialize.return_value = True
    first = asyncio.Event()
    release = asyncio.Event()
    second = asyncio.Event()
    calls = 0

    async def reconcile():
        nonlocal calls
        calls += 1
        if calls == 1:
            first.set()
            await release.wait()
        else:
            second.set()
        return SimpleNamespace(pages=1, persisted=1)

    sync = FillSync(store, ws, reconciler=SimpleNamespace(reconcile=reconcile), poll_interval=0.001)
    task = asyncio.create_task(sync.run())
    try:
        await asyncio.wait_for(first.wait(), 1)
        ws.connection_generation = 2
        await asyncio.sleep(0.01)
        assert calls == 1
        release.set()
        await asyncio.wait_for(second.wait(), 1)
        assert calls == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_database_failure_retries_and_recovers(caplog):
    ws = SimpleNamespace(is_connected=True, connection_generation=1)
    store = MagicMock()
    store.initialize.side_effect = [False, True]
    completed = asyncio.Event()

    async def reconcile():
        completed.set()
        return SimpleNamespace(pages=1, persisted=0)

    sync = FillSync(store, ws, reconciler=SimpleNamespace(reconcile=reconcile), retry_interval=0.01, poll_interval=0.001)
    task = asyncio.create_task(sync.run())
    try:
        await asyncio.wait_for(completed.wait(), 1)
        assert store.initialize.call_count == 2
        assert "Fill reconciliation failed" in caplog.text
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)