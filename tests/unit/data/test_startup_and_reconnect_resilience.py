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


@pytest.mark.asyncio
async def test_market_maker_reconnect_delayed_recovery_scans_from_session_start():
    """Verify MarketMaker recovery scans from session start even if recovery is delayed by minutes."""
    bot = AvellanedaStoikovBot(ticker="TEST")
    bot.inv_manager.fill_store = MagicMock()
    bot.inv_manager.fill_store.initialize = MagicMock(return_value=True)
    bot.ws_client.is_connected = True
    bot.ws_client.connection_generation = 2

    # Set session start 1 hour in the past
    session_start = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)
    bot.inv_manager._session_start_time = session_start

    bot._cancel_all_quotes = AsyncMock(return_value=True)
    bot.inv_manager.hydrate = AsyncMock(return_value=True)
    bot.om.sync_and_recover_state = AsyncMock()

    mock_report = MagicMock(pages=1, persisted=0)
    with patch("data.fill_reconciliation.FillReconciler.reconcile", new_callable=AsyncMock, return_value=mock_report) as mock_reconcile:
        await bot._handle_connection_change(old_generation=1)

    # Reconciler must be called with start=session_start, NOT truncated to 60 seconds
    mock_reconcile.assert_awaited_once_with(start=session_start)


@pytest.mark.asyncio
async def test_concurrent_live_websocket_and_rest_fill_delivery():
    """Verify concurrent delivery of the same fill via live WebSocket and REST recovery is serialized and applied once."""
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

    # Use cross-field aliased identifiers (WS trade_id matches REST fill_id)
    ws_payload = {
        "trade_id": "cross-race-id",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.50",
        "fee_dollars": "0.10",
    }
    rest_payload = {
        "fill_id": "cross-race-id",
        "trade_id": "rest-market-trade-race",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.50",
        "fee_dollars": "0.10",
        "created_time": "2026-11-22T12:05:00Z",
    }

    # Barrier forces both threads to synchronize and race concurrently for _fill_lock
    barrier = threading.Barrier(2)
    real_lock = manager._fill_lock

    class BarrierLock:
        def __enter__(self):
            try:
                barrier.wait(timeout=3.0)
            except threading.BrokenBarrierError:
                pass
            return real_lock.__enter__()

        def __exit__(self, *args):
            return real_lock.__exit__(*args)

    manager._fill_lock = BarrierLock()

    # Task 1: Live WebSocket delivery via _handle_message on worker thread
    async def live_delivery():
        return await asyncio.to_thread(asyncio.run, manager._handle_message({"type": "fill", "msg": dict(ws_payload)}))

    # Task 2: REST delivery via apply_reconciled_fill running in a worker thread
    async def rest_delivery():
        return await asyncio.to_thread(manager.apply_reconciled_fill, dict(rest_payload))

    results = await asyncio.gather(live_delivery(), rest_delivery(), return_exceptions=True)

    # Restore lock
    manager._fill_lock = real_lock

    # Neither should have raised an exception
    for r in results:
        if isinstance(r, Exception):
            raise r

    # Accounting invariants: fill applied exactly once
    assert manager._fill_count == 1
    assert pnl.get_total_fees("KXTEST") == 10.0
    assert manager.get_position("KXTEST") == 10


@pytest.mark.asyncio
async def test_inventory_manager_cross_field_identity_aliasing_websocket_then_rest():
    """Verify WebSocket fill with trade_id='X' deduplicates REST fill with fill_id='X', trade_id='Y'."""
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

    ws_fill = {
        "trade_id": "fill-abc",
        "order_id": "ord-1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price_dollars": "0.50",
        "fee_dollars": "0.05",
    }
    rest_fill = {
        "fill_id": "fill-abc",
        "trade_id": "trade-def",
        "order_id": "ord-1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price_dollars": "0.50",
        "fee_dollars": "0.05",
        "created_time": "2026-11-22T12:05:00Z",
    }

    # 1. Live WebSocket arrives first
    await manager._handle_message({"type": "fill", "msg": ws_fill})
    assert manager._fill_count == 1
    assert manager.get_position("KXTEST") == 5
    assert pnl.get_total_fees("KXTEST") == 5.0

    # 2. Reconciled REST fill arrives later with fill_id='fill-abc' and distinct trade_id='trade-def'
    applied = manager.apply_reconciled_fill(rest_fill)
    assert applied is False  # Deduplicated via cross-field aliasing
    assert manager._fill_count == 1  # Not double counted
    assert manager.get_position("KXTEST") == 5
    assert pnl.get_total_fees("KXTEST") == 5.0


@pytest.mark.asyncio
async def test_inventory_manager_cross_field_identity_aliasing_rest_then_websocket():
    """Verify REST fill with fill_id='X', trade_id='Y' deduplicates WebSocket fill with trade_id='X'."""
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)

    rest_fill = {
        "fill_id": "fill-xyz",
        "trade_id": "trade-uvw",
        "order_id": "ord-2",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price_dollars": "0.50",
        "fee_dollars": "0.05",
        "created_time": "2026-11-22T12:05:00Z",
    }
    ws_fill = {
        "trade_id": "fill-xyz",
        "order_id": "ord-2",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 5,
        "price_dollars": "0.50",
        "fee_dollars": "0.05",
    }

    # 1. REST recovery arrives first
    applied = manager.apply_reconciled_fill(rest_fill)
    assert applied is True
    assert manager._fill_count == 1
    assert manager.get_position("KXTEST") == 5
    assert pnl.get_total_fees("KXTEST") == 5.0

    # 2. Live WebSocket arrives later with trade_id matching REST fill_id
    await manager._handle_message({"type": "fill", "msg": ws_fill})
    assert manager._fill_count == 1  # Deduplicated, not double counted
    assert manager.get_position("KXTEST") == 5
    assert pnl.get_total_fees("KXTEST") == 5.0


def test_inventory_manager_conflicting_identities_rejected():
    """Verify fills with conflicting order_id or conflicting trade_id are rejected."""
    ws = MagicMock()
    manager = InventoryManager(ws)

    fill1 = {
        "fill_id": "f-base",
        "trade_id": "t-base",
        "order_id": "o-base",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.50",
        "fee_dollars": "0.01",
    }
    assert manager.apply_reconciled_fill(fill1) is True

    # Conflicting order_id on same fill_id
    fill_bad_order = dict(fill1, order_id="o-different")
    with pytest.raises(ValueError, match="Conflicting order_id"):
        manager.apply_reconciled_fill(fill_bad_order)

    # Conflicting trade_id on same fill_id (not an alias match)
    fill_bad_trade = dict(fill1, trade_id="t-different")
    with pytest.raises(ValueError, match="Conflicting trade_id"):
        manager.apply_reconciled_fill(fill_bad_trade)


@pytest.mark.asyncio
async def test_reconciler_replays_multi_page_fills_in_chronological_order():
    """Verify FillReconciler replays recovered fills in chronological (FIFO) order into on_fill."""
    store = MagicMock()
    store.record = MagicMock(return_value=True)

    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)

    # Three fills:
    # 1. Buy 10 @ 40c (t=10:00:00)
    # 2. Sell 10 @ 60c (t=10:01:00) -> Realized PnL = +200c
    # 3. Buy 10 @ 50c (t=10:02:00) -> New open lot @ 50c
    fill1 = {
        "fill_id": "f1",
        "trade_id": "t1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.40",
        "fee_dollars": "0.00",
        "created_time": "2026-11-22T10:00:00Z",
    }
    fill2 = {
        "fill_id": "f2",
        "trade_id": "t2",
        "market_ticker": "KXTEST",
        "action": "sell",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.60",
        "fee_dollars": "0.00",
        "created_time": "2026-11-22T10:01:00Z",
    }
    fill3 = {
        "fill_id": "f3",
        "trade_id": "t3",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.50",
        "fee_dollars": "0.00",
        "created_time": "2026-11-22T10:02:00Z",
    }

    # Kalshi REST returns fills in reverse chronological order (newest first)
    # Page 1: fill3, fill2; Page 2: fill1
    page1 = {"fills": [fill3, fill2], "cursor": "page2"}
    page2 = {"fills": [fill1], "cursor": None}

    reconciler = FillReconciler(
        store,
        "https://example.invalid",
        lambda m, p: {},
        min_interval=0,
        on_fill=manager.apply_reconciled_fill,
    )

    def mock_get(url, **kwargs):
        params = kwargs.get("params", {})
        cursor = params.get("cursor")
        resp = MagicMock()
        resp.status_code = 200
        if "/historical/fills" in url:
            resp.json.return_value = {"fills": [], "cursor": None}
        elif cursor == "page2":
            resp.json.return_value = page2
        else:
            resp.json.return_value = page1
        return resp

    reconciler.session = MagicMock()
    reconciler.session.get = MagicMock(side_effect=mock_get)

    report = await reconciler.reconcile(start=datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc))

    assert report.persisted == 3
    # FIFO Realized PnL: sell 10 @ 60c matches buy 10 @ 40c => +200c ($2.00)
    # (If replayed in reverse order, sell @ 60c matches buy @ 50c => +100c)
    assert manager.get_realized_pnl("KXTEST") == 200.0
    assert manager.get_position("KXTEST") == 10


@pytest.mark.asyncio
async def test_interleaved_live_and_recovered_fills_rebuilds_chronological_fifo_accounting():
    """
    Verify that when a later live fill is applied before earlier missed fills are recovered,
    accounting is rebuilt in chronological order across both live and recovered fills.

    Exchange chronological fills:
    1. Buy 1 @ 40c (t=10:00:00)
    2. Sell 1 @ 60c (t=10:01:00) -> Realized PnL = +20c ($0.20)
    3. Buy 1 @ 50c (t=10:02:00) -> Open lot @ 50c

    Delivery order:
    1. Fill 3 arrives live first via WebSocket.
    2. Fill 1 and Fill 2 are recovered via REST afterward.

    Expected result:
    - Realized PnL must be 20c (NOT 10c).
    - Open inventory must be 1 contract with cost basis 50c (NOT 40c).
    - Net position must be 1.
    """
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 23, 9, 0, 0, tzinfo=timezone.utc)

    # Fill 3 (t=10:02:00) - arrives live first
    fill3 = {
        "fill_id": "f3",
        "trade_id": "t3",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.50",
        "fee_dollars": "0.00",
        "created_time": "2026-11-23T10:02:00Z",
    }
    # Fill 1 (t=10:00:00) - recovered later
    fill1 = {
        "fill_id": "f1",
        "trade_id": "t1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.40",
        "fee_dollars": "0.00",
        "created_time": "2026-11-23T10:00:00Z",
    }
    # Fill 2 (t=10:01:00) - recovered later
    fill2 = {
        "fill_id": "f2",
        "trade_id": "t2",
        "market_ticker": "KXTEST",
        "action": "sell",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.60",
        "fee_dollars": "0.00",
        "created_time": "2026-11-23T10:01:00Z",
    }

    # Step 1: Live fill 3 arrives first
    await manager._handle_message({"type": "fill", "msg": fill3})
    assert manager.get_position("KXTEST") == 1
    assert manager.get_realized_pnl("KXTEST") == 0.0

    # Step 2: REST recovery delivers earlier fills fill1 and fill2
    assert manager.apply_reconciled_fill(fill1) is True
    assert manager.apply_reconciled_fill(fill2) is True

    # Assert FIFO accounting reflects true chronological execution:
    # Sell @ 60c matches Buy @ 40c => Realized PnL = +20c ($0.20)
    assert manager.get_realized_pnl("KXTEST") == 20.0
    assert manager.get_position("KXTEST") == 1

    # Assert remaining open lot is the 50c buy, NOT the 40c buy
    market = pnl.get_or_create_market("KXTEST")
    assert len(market.open_lots) == 1
    assert market.open_lots[0].price_cents == 50.0
    assert market.open_lots[0].count == 1


@pytest.mark.asyncio
async def test_interleaved_live_and_recovered_fills_multi_contract_and_fees():
    """Verify chronological replay with multi-contract fills and allocated fees."""
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 23, 9, 0, 0, tzinfo=timezone.utc)

    # 1. Buy 10 @ 40c, fee 0.10 ($0.10) (t=10:00:00)
    fill1 = {
        "fill_id": "multi-f1",
        "trade_id": "multi-t1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.40",
        "fee_dollars": "0.10",
        "created_time": "2026-11-23T10:00:00Z",
    }
    # 2. Sell 10 @ 60c, fee 0.10 ($0.10) (t=10:01:00)
    fill2 = {
        "fill_id": "multi-f2",
        "trade_id": "multi-t2",
        "market_ticker": "KXTEST",
        "action": "sell",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.60",
        "fee_dollars": "0.10",
        "created_time": "2026-11-23T10:01:00Z",
    }
    # 3. Buy 10 @ 50c, fee 0.10 ($0.10) (t=10:02:00)
    fill3 = {
        "fill_id": "multi-f3",
        "trade_id": "multi-t3",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 10,
        "price_dollars": "0.50",
        "fee_dollars": "0.10",
        "created_time": "2026-11-23T10:02:00Z",
    }

    # Live fill 3 arrives first
    await manager._handle_message({"type": "fill", "msg": fill3})

    # REST recovery delivers fill1 and fill2 afterward
    assert manager.apply_reconciled_fill(fill1) is True
    assert manager.apply_reconciled_fill(fill2) is True

    # Gross PnL = 10 * (60c - 40c) = 200c ($2.00)
    # Net Realized PnL = 200c - 10c (entry fee) - 10c (exit fee) = 180c ($1.80)
    assert manager.get_realized_pnl("KXTEST") == 180.0
    assert manager.get_position("KXTEST") == 10
    assert pnl.get_total_fees("KXTEST") == 30.0

    market = pnl.get_or_create_market("KXTEST")
    assert len(market.open_lots) == 1
    assert market.open_lots[0].price_cents == 50.0
    assert market.open_lots[0].count == 10
    assert market.open_lots[0].entry_fee_per_contract == 1.0


def test_arbitrary_arrival_permutations_yield_identical_fifo_realized_pnl():
    """Verify all 6 arrival permutations of 3 trades yield identical chronological realized PnL and inventory."""
    import itertools

    trades = [
        {
            "fill_id": "perm-f1",
            "trade_id": "perm-t1",
            "market_ticker": "KXTEST",
            "action": "buy",
            "side": "yes",
            "count": 1,
            "price_dollars": "0.40",
            "fee_dollars": "0.00",
            "created_time": "2026-11-23T10:00:00Z",
        },
        {
            "fill_id": "perm-f2",
            "trade_id": "perm-t2",
            "market_ticker": "KXTEST",
            "action": "sell",
            "side": "yes",
            "count": 1,
            "price_dollars": "0.60",
            "fee_dollars": "0.00",
            "created_time": "2026-11-23T10:01:00Z",
        },
        {
            "fill_id": "perm-f3",
            "trade_id": "perm-t3",
            "market_ticker": "KXTEST",
            "action": "buy",
            "side": "yes",
            "count": 1,
            "price_dollars": "0.50",
            "fee_dollars": "0.00",
            "created_time": "2026-11-23T10:02:00Z",
        },
    ]

    for perm in itertools.permutations(trades):
        ws = MagicMock()
        pnl = PnLTracker()
        manager = InventoryManager(ws, pnl_tracker=pnl)
        manager._session_start_time = datetime(2026, 11, 23, 9, 0, 0, tzinfo=timezone.utc)

        for trade in perm:
            applied = manager.apply_reconciled_fill(dict(trade))
            assert applied is True

        # In all permutations:
        # Trade 1 (Buy @ 40c) and Trade 2 (Sell @ 60c) offset for +20c realized PnL.
        # Trade 3 (Buy @ 50c) remains open.
        assert manager.get_realized_pnl("KXTEST") == 20.0
        assert manager.get_position("KXTEST") == 1
        market = pnl.get_or_create_market("KXTEST")
        assert len(market.open_lots) == 1
        assert market.open_lots[0].price_cents == 50.0
        assert market.open_lots[0].count == 1


@pytest.mark.asyncio
async def test_rotation_session_accounting_rebuild_prevents_double_counting():
    """
    Verify that rotating away from and returning to a traded ticker preserves the
    session accounting baseline without double-counting prior realized PnL or fees
    during a reconnect rebuild.

    Scenario:
    1. Session 1: Buy 1 @ 40c, Sell 1 @ 60c (fees 5c each).
       -> Cumulative Realized: +10c net ($0.10), Total Fees: 10c ($0.10).
       -> Session 1 Realized: +10c, Session 1 Fees: 10c.
    2. Slate rotation: rotate away and later return to ticker (reset_market_session).
       -> Session baseline saved at Realized = 10c, Fees = 10c.
       -> Session 2 Realized Delta = 0c, Session 2 Fees = 0c.
    3. Reconnect occurs -> rebuild_accounting() runs.
       -> Must NOT double-count: Cumulative Realized must remain 10c (NOT 20c).
       -> Session 2 Realized must remain 0c (NOT 10c).
       -> Total Fees must remain 10c (NOT 20c).
    4. Session 2 trading with out-of-order fills:
       -> Live fill: Buy 1 @ 50c (fee 5c, t=10:02:00) arrives first.
       -> REST recovery: Buy 1 @ 30c (fee 5c, t=10:00:00) & Sell 1 @ 70c (fee 5c, t=10:01:00).
       -> Net Session 2 Realized: (70 - 30) - 10 = +30c.
       -> Cumulative Realized: 10c + 30c = 40c.
       -> Session 2 Realized: 40c - 10c = 30c.
       -> Total Fees: 10c + 15c = 25c.
       -> Session 2 Fees: 25c - 10c = 15c.
       -> Open lot: 1 contract @ 50c.
    """
    ws = MagicMock()
    pnl = PnLTracker()
    manager = InventoryManager(ws, pnl_tracker=pnl)
    manager._session_start_time = datetime(2026, 11, 23, 9, 0, 0, tzinfo=timezone.utc)

    # 1. Session 1 trades
    fill1 = {
        "fill_id": "s1-f1",
        "trade_id": "s1-t1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.40",
        "fee_dollars": "0.05",
        "created_time": "2026-11-23T09:30:00Z",
    }
    fill2 = {
        "fill_id": "s1-f2",
        "trade_id": "s1-t2",
        "market_ticker": "KXTEST",
        "action": "sell",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.60",
        "fee_dollars": "0.05",
        "created_time": "2026-11-23T09:31:00Z",
    }
    await manager._handle_message({"type": "fill", "msg": fill1})
    await manager._handle_message({"type": "fill", "msg": fill2})

    s1_summary = pnl.get_market_summary("KXTEST")
    assert s1_summary["realized_pnl_cents"] == 10.0  # (60 - 40) - 5 (entry) - 5 (exit)
    assert s1_summary["total_fees_cents"] == 10.0
    assert s1_summary["session_realized_pnl_cents"] == 10.0
    assert s1_summary["session_fees_cents"] == 10.0
    assert manager.get_position("KXTEST") == 0

    # 2. Slate rotation: rotate away and later return to ticker
    manager.reset_market_session("KXTEST")

    s2_initial = pnl.get_market_summary("KXTEST")
    assert s2_initial["realized_pnl_cents"] == 10.0
    assert s2_initial["total_fees_cents"] == 10.0
    assert s2_initial["session_realized_pnl_cents"] == 0.0
    assert s2_initial["session_fees_cents"] == 0.0

    # 3. Reconnect occurs -> rebuild_accounting()
    manager.rebuild_accounting("KXTEST")

    s2_rebuilt = pnl.get_market_summary("KXTEST")
    assert s2_rebuilt["realized_pnl_cents"] == 10.0  # MUST NOT be 20c
    assert s2_rebuilt["total_fees_cents"] == 10.0     # MUST NOT be 20c
    assert s2_rebuilt["session_realized_pnl_cents"] == 0.0  # MUST NOT be 10c
    assert s2_rebuilt["session_fees_cents"] == 0.0          # MUST NOT be 10c
    assert manager.get_position("KXTEST") == 0

    # 4. Session 2 trading with out-of-order fill arrivals
    s2_fill3 = {
        "fill_id": "s2-f3",
        "trade_id": "s2-t3",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.50",
        "fee_dollars": "0.05",
        "created_time": "2026-11-23T10:02:00Z",
    }
    s2_fill1 = {
        "fill_id": "s2-f1",
        "trade_id": "s2-t1",
        "market_ticker": "KXTEST",
        "action": "buy",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.30",
        "fee_dollars": "0.05",
        "created_time": "2026-11-23T10:00:00Z",
    }
    s2_fill2 = {
        "fill_id": "s2-f2",
        "trade_id": "s2-t2",
        "market_ticker": "KXTEST",
        "action": "sell",
        "side": "yes",
        "count": 1,
        "price_dollars": "0.70",
        "fee_dollars": "0.05",
        "created_time": "2026-11-23T10:01:00Z",
    }

    # Live fill 3 arrives first
    await manager._handle_message({"type": "fill", "msg": s2_fill3})

    # REST recovery delivers fill 1 and fill 2 out-of-order afterward
    assert manager.apply_reconciled_fill(s2_fill1) is True
    assert manager.apply_reconciled_fill(s2_fill2) is True

    s2_final = pnl.get_market_summary("KXTEST")
    # Gross PnL in session 2: 70 - 30 = 40c. Net of fees (5c entry + 5c exit): 30c.
    # Cumulative Realized PnL: 10c (session 1) + 30c (session 2) = 40c
    assert s2_final["realized_pnl_cents"] == 40.0
    # Session 2 Realized PnL: 40c - 10c (baseline) = 30c
    assert s2_final["session_realized_pnl_cents"] == 30.0
    # Total Fees: 10c (session 1) + 15c (session 2) = 25c
    assert s2_final["total_fees_cents"] == 25.0
    # Session 2 Fees: 25c - 10c (baseline) = 15c
    assert s2_final["session_fees_cents"] == 15.0
    # Net position is 1 contract
    assert manager.get_position("KXTEST") == 1
    # Open lot is the unclosed 50c buy
    market = pnl.get_or_create_market("KXTEST")
    assert len(market.open_lots) == 1
    assert market.open_lots[0].price_cents == 50.0
    assert market.open_lots[0].count == 1


