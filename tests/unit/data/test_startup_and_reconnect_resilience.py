import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.fill_spool import FillSpool
from data.inventory_manager import InventoryManager
from data.websocket_client import FatalIngestionError
from strategy.market_maker import AvellanedaStoikovBot
from utils.alerting import reset_alert_history, send_alert_sync


@pytest.mark.asyncio
async def test_reconcile_dirty_spool_no_op_when_clean(tmp_path, monkeypatch):
    """Verify reconcile_dirty_spool returns True and does nothing when spool is clean."""
    spool_path = tmp_path / "spool.db"
    monkeypatch.setenv("FILL_SPOOL_PATH", str(spool_path))

    spool = FillSpool(spool_path)
    assert not spool.dirty()
    spool.close()

    ws = MagicMock()
    store = MagicMock()
    manager = InventoryManager(ws, fill_store=store)

    result = await manager.reconcile_dirty_spool()
    assert result is True
    store.record.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_dirty_spool_drains_fills_and_clears_dirty_latch(tmp_path, monkeypatch):
    """Verify reconcile_dirty_spool drains pending fills, reconciles REST fills, and clears dirty flag."""
    spool_path = tmp_path / "spool.db"
    monkeypatch.setenv("FILL_SPOOL_PATH", str(spool_path))

    # Pre-populate dirty spool with 2 fills
    spool = FillSpool(spool_path)
    fill1 = {"trade_id": "t1", "market_ticker": "T", "side": "yes", "count": 2, "price": 50}
    fill2 = {"trade_id": "t2", "market_ticker": "T", "side": "no", "count": 1, "price": 40}
    spool.append(fill1)
    spool.append(fill2)
    spool.set_dirty()
    assert spool.dirty()
    spool.close()

    ws = MagicMock()
    ws.subscribe = AsyncMock()
    store = MagicMock()
    store.initialize = MagicMock(return_value=True)
    store.record = MagicMock(return_value=True)

    mock_report = MagicMock(pages=1, persisted=0)
    mock_reconciler = MagicMock()
    mock_reconciler.reconcile = AsyncMock(return_value=mock_report)

    manager = InventoryManager(ws, fill_store=store)
    result = await manager.reconcile_dirty_spool(reconciler=mock_reconciler)
    assert result is True

    # Check that spool is now clean and has 0 pending fills
    check_spool = FillSpool(spool_path)
    assert not check_spool.dirty()
    assert check_spool.pending() is None
    check_spool.close()

    assert store.record.call_count == 2
    mock_reconciler.reconcile.assert_awaited_once()

    # Now verify that subscribe() can proceed cleanly without FatalIngestionError
    await manager.subscribe()
    assert manager._fill_spool is not None
    await manager.close_fill_ingestion(clean=True)


@pytest.mark.asyncio
async def test_reconcile_dirty_spool_retains_dirty_on_store_failure(tmp_path, monkeypatch):
    """Verify reconcile_dirty_spool retains dirty flag if store fails to record fill."""
    spool_path = tmp_path / "spool.db"
    monkeypatch.setenv("FILL_SPOOL_PATH", str(spool_path))

    spool = FillSpool(spool_path)
    spool.append({"trade_id": "t1", "market_ticker": "T", "side": "yes", "count": 1, "price": 50})
    spool.set_dirty()
    spool.close()

    ws = MagicMock()
    store = MagicMock()
    store.initialize = MagicMock(return_value=True)
    store.record = MagicMock(return_value=False)  # Persistence failure

    manager = InventoryManager(ws, fill_store=store)
    result = await manager.reconcile_dirty_spool()
    assert result is False

    check_spool = FillSpool(spool_path)
    assert check_spool.dirty()
    check_spool.close()


@pytest.mark.asyncio
async def test_reconcile_dirty_spool_retains_dirty_on_reconciler_error(tmp_path, monkeypatch):
    """Verify reconcile_dirty_spool retains dirty flag if REST reconciler raises an error."""
    spool_path = tmp_path / "spool.db"
    monkeypatch.setenv("FILL_SPOOL_PATH", str(spool_path))

    spool = FillSpool(spool_path)
    spool.set_dirty()
    spool.close()

    ws = MagicMock()
    store = MagicMock()
    store.initialize = MagicMock(return_value=True)

    mock_reconciler = MagicMock()
    mock_reconciler.reconcile = AsyncMock(side_effect=RuntimeError("Exchange 503"))

    manager = InventoryManager(ws, fill_store=store)
    result = await manager.reconcile_dirty_spool(reconciler=mock_reconciler)
    assert result is False

    check_spool = FillSpool(spool_path)
    assert check_spool.dirty()
    check_spool.close()


@pytest.mark.asyncio
async def test_market_maker_in_flight_reconnect_recovers_without_crashing():
    """Verify MarketMaker recovers from in-flight WebSocket reconnect without throwing FatalIngestionError."""
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = MagicMock()
    bot.inv_manager.fill_store.initialize = MagicMock(return_value=True)
    bot.ws_client.is_connected = True
    bot.ws_client.connection_generation = 2

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager.hydrate = AsyncMock(return_value=True)
    bot.om.sync_and_recover_state = AsyncMock()

    mock_report = MagicMock(pages=1, persisted=0)
    with patch("data.fill_reconciliation.FillReconciler.reconcile", new_callable=AsyncMock, return_value=mock_report):
        new_gen = await bot._handle_connection_change(old_generation=1)

    assert new_gen == 2
    assert bot._fill_connection_generation == 2
    bot._cancel_all_quotes.assert_awaited_once()
    bot.inv_manager.hydrate.assert_awaited_once_with(is_startup=False)
    bot.om.sync_and_recover_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_market_maker_in_flight_reconnect_fails_closed_when_hydration_fails():
    """Verify MarketMaker raises FatalIngestionError if post-reconnect hydration fails."""
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = MagicMock()
    bot.inv_manager.fill_store.initialize = MagicMock(return_value=True)
    bot.ws_client.is_connected = True
    bot.ws_client.connection_generation = 2

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager.hydrate = AsyncMock(return_value=False)  # Hydration rejected
    bot.om.sync_and_recover_state = AsyncMock()

    mock_report = MagicMock(pages=1, persisted=0)
    with patch("data.fill_reconciliation.FillReconciler.reconcile", new_callable=AsyncMock, return_value=mock_report):
        with pytest.raises(FatalIngestionError, match="Failed to re-hydrate"):
            await bot._handle_connection_change(old_generation=1)


def test_send_alert_sync_throttling():
    """Verify send_alert_sync throttles repeated identical alerts within cooldown window."""
    reset_alert_history()
    with patch("utils.alerting.ALERT_WEBHOOK_URL", "https://mock.webhook.test/alerts"), \
         patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)

        # First alert delivers
        send_alert_sync("Order manager emergency shutdown initiated.", cooldown_seconds=60)
        assert mock_post.call_count == 1

        # Second identical alert within cooldown is suppressed
        send_alert_sync("Order manager emergency shutdown initiated.", cooldown_seconds=60)
        assert mock_post.call_count == 1
