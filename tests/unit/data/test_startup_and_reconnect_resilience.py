import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from data.fill_spool import FillSpool
from data.fill_reconciliation import FillReconciler, ReconciliationError
from data.inventory_manager import InventoryManager
from data.pnl_tracker import PnLTracker
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
    bot.inv_manager.hydrate.assert_awaited_once_with(is_startup=False, require_complete=True)
    bot.om.sync_and_recover_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_market_maker_in_flight_reconnect_fails_closed_when_quotes_unconfirmed():
    """Verify MarketMaker escalates to KillSwitch if quote withdrawal is unconfirmed."""
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot._cancel_all_quotes = AsyncMock(return_value=False)  # Cancellation failed
    bot._escalate_to_kill_switch = AsyncMock()

    with pytest.raises(FatalIngestionError, match="Failed to confirm quote withdrawal"):
        await bot._handle_connection_change(old_generation=1)

    bot._escalate_to_kill_switch.assert_awaited_once_with(context="unconfirmed quote withdrawal during reconnect")


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


@pytest.mark.asyncio
async def test_market_maker_in_flight_reconnect_fails_closed_on_connection_change_during_recovery():
    """Verify MarketMaker escalates if connection generation changes during recovery REST calls."""
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = MagicMock()
    bot.inv_manager.fill_store.initialize = MagicMock(return_value=True)
    bot.ws_client.is_connected = True
    bot.ws_client.connection_generation = 2

    bot._cancel_all_quotes = AsyncMock(return_value=True)

    async def hydrate_side_effect(*args, **kwargs):
        # Socket disconnected or reconnected during hydration
        bot.ws_client.connection_generation = 3
        return True

    bot.inv_manager.hydrate = AsyncMock(side_effect=hydrate_side_effect)
    bot.om.sync_and_recover_state = AsyncMock()

    mock_report = MagicMock(pages=1, persisted=0)
    with patch("data.fill_reconciliation.FillReconciler.reconcile", new_callable=AsyncMock, return_value=mock_report):
        with pytest.raises(FatalIngestionError, match="WebSocket connection changed during in-flight recovery"):
            await bot._handle_connection_change(old_generation=1)


def test_apply_reconciled_fill_updates_pnl_tracker_and_deduplicates():
    """Verify apply_reconciled_fill records fill in PnLTracker and ignores subsequent duplicates."""
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)

    fill_data = {
        "fill_id": "f-101",
        "trade_id": "t-101",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price_dollars": "0.45",
        "fee_dollars": "0.05",
    }

    # First application applies fill, increments fee and position
    applied = manager.apply_reconciled_fill(fill_data)
    assert applied is True
    assert pnl.get_total_fees("KXTEST") == 5.0
    assert manager.get_position("KXTEST") == 5

    # Second application of identical fill is deduplicated
    applied_again = manager.apply_reconciled_fill(fill_data)
    assert applied_again is False
    assert pnl.get_total_fees("KXTEST") == 5.0  # Fees not double counted
    assert manager.get_position("KXTEST") == 5


@pytest.mark.asyncio
async def test_hydrate_strict_recovery_requires_both_snapshots():
    """Verify hydrate(is_startup=False, require_complete=True) fails if positions endpoint fails."""
    ws = MagicMock()
    manager = InventoryManager(ws)

    # Balance succeeds, positions fails (returns None)
    manager._fetch_balance = MagicMock(return_value=10000)
    manager._fetch_positions = MagicMock(return_value=None)

    # In periodic mode (require_complete=False), partial snapshot is allowed
    periodic_ok = await manager.hydrate(is_startup=False, require_complete=False)
    assert periodic_ok is True

    # In strict recovery mode (require_complete=True), missing positions fails closed
    recovery_ok = await manager.hydrate(is_startup=False, require_complete=True)
    assert recovery_ok is False


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


def test_apply_reconciled_fill_ignores_historical_fills_prior_to_session_start():
    """Verify apply_reconciled_fill ignores historical fills dated before session start."""
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Historical fill before session start should be dropped
    historical_fill = {
        "fill_id": "f-old",
        "trade_id": "t-old",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price_dollars": "0.45",
        "fee_dollars": "0.05",
        "created_time": "2026-11-22T11:59:59Z",
    }
    applied_old = manager.apply_reconciled_fill(historical_fill)
    assert applied_old is False
    assert pnl.get_total_fees("KXTEST") == 0.0
    assert manager.get_position("KXTEST") == 0

    # 2. Session fill after session start should be applied
    session_fill = {
        "fill_id": "f-new",
        "trade_id": "t-new",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price_dollars": "0.45",
        "fee_dollars": "0.05",
        "created_time": "2026-11-22T12:00:01Z",
    }
    applied_new = manager.apply_reconciled_fill(session_fill)
    assert applied_new is True
    assert pnl.get_total_fees("KXTEST") == 5.0
    assert manager.get_position("KXTEST") == 5


def test_apply_reconciled_fill_concurrent_delivery_is_threadsafe():
    """Verify concurrent delivery of identical fill via REST and live threads applies exactly once."""
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

    fill_payload = {
        "fill_id": "f-race-1",
        "trade_id": "t-race-1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.50",
        "fee_dollars": "0.10",
        "created_time": "2026-11-22T12:05:00Z",
    }

    results = []
    def runner():
        try:
            res = manager.apply_reconciled_fill(dict(fill_payload))
            results.append(res)
        except Exception as e:
            results.append(e)

    threads = [threading.Thread(target=runner) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one thread should successfully apply the fill; others should return False
    assert results.count(True) == 1
    assert results.count(False) == 9
    assert pnl.get_total_fees("KXTEST") == 10.0
    assert manager.get_position("KXTEST") == 10


@pytest.mark.asyncio
async def test_reconciler_on_fill_failure_escalates_and_fails_closed():
    """Verify FillReconciler fails closed with ReconciliationError when on_fill callback fails."""
    store = MagicMock()
    store.record = MagicMock(return_value=True)

    def failing_callback(fill):
        raise ValueError("Accounting engine arithmetic overflow")

    reconciler = FillReconciler(
        store,
        "https://example.invalid",
        lambda m, p: {},
        min_interval=0,
        on_fill=failing_callback,
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "fills": [{"fill_id": "f-err", "created_time": "2026-11-22T12:00:00Z"}],
        "cursor": None,
    }
    reconciler.session = MagicMock()
    reconciler.session.get = MagicMock(return_value=mock_response)

    with pytest.raises(ReconciliationError, match="Accounting engine arithmetic overflow") as exc_info:
        await reconciler.reconcile()

    assert exc_info.value.report.failed == 1
    assert any("Fill accounting callback failed" in w for w in exc_info.value.report.warnings)


def test_concurrent_alerts_prevent_duplicate_dispatches():
    """Verify concurrent alert calls across threads synchronize reservation and prevent duplicate posts."""
    reset_alert_history()
    with patch("utils.alerting.ALERT_WEBHOOK_URL", "https://mock.webhook.test/alerts"), \
         patch("requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)

        def send_call():
            send_alert_sync("Concurrent alert test event", cooldown_seconds=300)

        threads = [threading.Thread(target=send_call) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 1 HTTP POST should have been dispatched; remaining 9 were throttled
        assert mock_post.call_count == 1
