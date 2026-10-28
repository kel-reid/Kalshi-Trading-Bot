"""
Unit tests for Exchange Shard Routing, Safe Order Cancellation,
Intra-Exchange Collateral Management, and Hedging Circuit Breakers.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from utils.market_api import get_market_exchange_index
from execution.order_manager import OrderManager
from execution.kill_switch import KillSwitch
from strategy.market_maker import AvellanedaStoikovBot


class MockResponse:
    def __init__(self, json_data, status_code=200):
        self.json_data = json_data
        self.status_code = status_code
        self.text = str(json_data)

    def json(self):
        return self.json_data


# ---------------------------------------------------------------------------
# 1. Market Shard Resolution
# ---------------------------------------------------------------------------
def test_get_market_exchange_index_success():
    with patch("utils.market_api.get_market_details", return_value={"exchange_index": 3, "ticker": "NBA-GAME-1"}):
        idx = get_market_exchange_index("NBA-GAME-1")
        assert idx == 3


def test_get_market_exchange_index_fallback_on_missing_or_error():
    with patch("utils.market_api.get_market_details", return_value={"ticker": "NFL-GAME-1"}):
        assert get_market_exchange_index("NFL-GAME-1") == 0

    with patch("utils.market_api.get_market_details", return_value=None):
        assert get_market_exchange_index("FAIL-TICKER") is None

    with patch("utils.market_api.get_market_details", side_effect=Exception("API Error")):
        assert get_market_exchange_index("ERR-TICKER") is None


def test_order_manager_get_ticker_exchange_index_does_not_cache_none():
    om = OrderManager()
    with patch("utils.market_api.get_market_details", return_value=None):
        idx = om.get_ticker_exchange_index("NBA-FAIL")
        assert idx is None
        assert "NBA-FAIL" not in om._ticker_exchange_indices

    # Subsequent recovery caches the verified shard
    with patch("utils.market_api.get_market_details", return_value={"exchange_index": 3}):
        idx = om.get_ticker_exchange_index("NBA-FAIL")
        assert idx == 3
        assert om._ticker_exchange_indices["NBA-FAIL"] == 3


# ---------------------------------------------------------------------------
# 2. OrderManager place_order with exchange_index
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_order_manager_place_order_sharded_payload():
    om = OrderManager()
    captured_payload = {}

    def fake_post(sign_path, payload):
        nonlocal captured_payload
        captured_payload = payload
        return MockResponse({
            "order": {
                "order_id": "kalshi-ord-sharded-123",
                "status": "resting",
                "exchange_index": 3,
                "ticker": "KXNBAGAME-TEST",
            }
        }, 201)

    om._post_request = fake_post

    order_id = await om.place_order(
        ticker="KXNBAGAME-TEST",
        side="yes",
        action="buy",
        count=5,
        price=45,
        exchange_index=3,
    )

    assert order_id is not None
    assert captured_payload.get("exchange_index") == 3
    assert captured_payload.get("ticker") == "KXNBAGAME-TEST"
    assert order_id in om.active_orders
    assert om.active_orders[order_id]["exchange_index"] == 3
    assert om.active_orders[order_id]["kalshi_order_id"] == "kalshi-ord-sharded-123"


# ---------------------------------------------------------------------------
# 3. OrderManager cancel_order with Shard Params and Zero-Trust 404
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_order_manager_cancel_order_sends_shard_query_params():
    om = OrderManager()
    om.active_orders["client-1"] = {
        "kalshi_order_id": "kalshi-123",
        "ticker": "KXNBAGAME-TEST",
        "exchange_index": 3,
        "side": "yes",
        "action": "buy",
        "count": 5,
        "price": 45,
    }

    captured_delete_params = {}
    def fake_delete(sign_path, params=None):
        nonlocal captured_delete_params
        captured_delete_params = params
        return MockResponse({"order": {"status": "canceled"}}, 200)

    om._delete_request = fake_delete

    success = await om.cancel_order("client-1")
    assert success is True
    assert "client-1" not in om.active_orders
    assert captured_delete_params == {"exchange_index": 3, "ticker": "KXNBAGAME-TEST", "market_ticker": "KXNBAGAME-TEST"}


@pytest.mark.asyncio
async def test_order_manager_cancel_order_404_retains_tracking_if_still_resting():
    """
    If DELETE returns 404 but get_order_status confirms the order is still RESTING
    (e.g. wrong shard or API anomaly), order tracking must NOT be cleared.
    """
    om = OrderManager()
    om.active_orders["client-1"] = {
        "kalshi_order_id": "kalshi-123",
        "ticker": "KXNBAGAME-TEST",
        "exchange_index": 0,  # Misspecified shard
    }

    # DELETE returns 404
    om._delete_request = MagicMock(return_value=MockResponse({"error": "not found"}, 404))
    # get_order_status confirms it is still resting on exchange!
    om.get_order_status = AsyncMock(return_value={
        "order_id": "kalshi-123",
        "status": "resting",
        "remaining_count": 5,
    })

    success = await om.cancel_order("client-1")
    assert success is False
    # CRITICAL INVARIANT: order tracking retained
    assert "client-1" in om.active_orders


@pytest.mark.asyncio
async def test_order_manager_cancel_order_404_clears_tracking_if_confirmed_closed():
    """
    If DELETE returns 404 and get_order_status confirms the order is executed/canceled,
    order tracking should be popped.
    """
    om = OrderManager()
    om.active_orders["client-1"] = {
        "kalshi_order_id": "kalshi-123",
        "ticker": "KXNBAGAME-TEST",
        "exchange_index": 3,
    }

    om._delete_request = MagicMock(return_value=MockResponse({"error": "not found"}, 404))
    om.get_order_status = AsyncMock(return_value={
        "order_id": "kalshi-123",
        "status": "executed",
        "remaining_count": 0,
    })

    success = await om.cancel_order("client-1")
    assert success is True
    assert "client-1" not in om.active_orders


@pytest.mark.asyncio
async def test_cancel_by_kalshi_id_verifies_terminal_status_on_404():
    om = OrderManager()
    om._delete_request = MagicMock(return_value=MockResponse({"error": "not found"}, 404))

    # Case A: status lookup failed (returns None) -> return False (fail closed)
    om.get_order_status = AsyncMock(return_value=None)
    res_none = await om._cancel_by_kalshi_id("kalshi-123", "client-1", exchange_index=3, ticker="NBA-1")
    assert res_none is False

    # Case B: status confirms order is still resting -> return False
    om.get_order_status = AsyncMock(return_value={"status": "resting", "remaining_count": 5})
    res_resting = await om._cancel_by_kalshi_id("kalshi-123", "client-1", exchange_index=3, ticker="NBA-1")
    assert res_resting is False

    # Case C: unknown / unverified status (e.g. "pending") -> return False
    om.get_order_status = AsyncMock(return_value={"status": "pending", "remaining_count": 0})
    res_unknown = await om._cancel_by_kalshi_id("kalshi-123", "client-1", exchange_index=3, ticker="NBA-1")
    assert res_unknown is False

    # Case D: status is canceled but subpenny remaining_count_fp is non-zero -> return False
    om.get_order_status = AsyncMock(return_value={"status": "canceled", "remaining_count": 0, "remaining_count_fp": "0.5"})
    res_fp = await om._cancel_by_kalshi_id("kalshi-123", "client-1", exchange_index=3, ticker="NBA-1")
    assert res_fp is False

    # Case E: status confirms order is canceled and zero remaining -> return True
    om.get_order_status = AsyncMock(return_value={"status": "canceled", "remaining_count": 0, "remaining_count_fp": "0.0"})
    res_canceled = await om._cancel_by_kalshi_id("kalshi-123", "client-1", exchange_index=3, ticker="NBA-1")
    assert res_canceled is True

    # Case F: status confirms order is executed and zero remaining -> return True
    om.get_order_status = AsyncMock(return_value={"status": "executed", "remaining_count": 0})
    res_executed = await om._cancel_by_kalshi_id("kalshi-123", "client-1", exchange_index=3, ticker="NBA-1")
    assert res_executed is True


# ---------------------------------------------------------------------------
# 4. Intra-Exchange Balance and Transfer
# ---------------------------------------------------------------------------
def test_order_manager_get_portfolio_balances_parsing():
    om = OrderManager()
    mock_resp = MockResponse({
        "balance": 3446,
        "balance_dollars": 34.46,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": 29.45},
            {"exchange_index": 1, "balance": 0.0},
            {"exchange_index": 2, "balance": 0.0},
            {"exchange_index": 3, "balance": 5.01},
        ]
    }, 200)

    with patch("requests.get", return_value=mock_resp):
        res = om.get_portfolio_balances()
        assert res["total_balance_dollars"] == 34.46
        assert res["shard_balances"][0] == 29.45
        assert res["shard_balances"][3] == 5.01


def test_order_manager_transfer_intra_exchange_execution():
    om = OrderManager()
    captured_payload = {}

    def fake_post(sign_path, payload):
        nonlocal captured_payload
        captured_payload = payload
        return MockResponse({"status": "complete"}, 201)

    om._post_request = fake_post

    success = om.transfer_intra_exchange(
        source_shard=0,
        destination_shard=3,
        amount_dollars=15.0,
    )

    assert success is True
    assert captured_payload["amount"] == 150000  # Centicents integer (10,000 per dollar)
    assert captured_payload["source_exchange_shard"] == 0
    assert captured_payload["destination_exchange_shard"] == 3
    assert "client_transfer_id" in captured_payload


@pytest.mark.asyncio
async def test_reconcile_resting_orders_propagates_fallback_shard_and_tickers():
    om = OrderManager()
    captured_cancel_kwargs = {}

    async def fake_cancel(kalshi_id, client_id=None, **kwargs):
        nonlocal captured_cancel_kwargs
        captured_cancel_kwargs = kwargs
        return True

    om._cancel_by_kalshi_id = fake_cancel

    # Exchange order returned without exchange_index in dictionary
    with patch.object(om, "fetch_resting_orders_sync", return_value=[
        {"order_id": "oid-untracked", "client_order_id": "cid-orphan", "ticker": "NBA-TEST"}
    ]):
        res = await om.reconcile_resting_orders(
            ticker="NBA-TEST",
            exchange_index=3
        )
        assert res == 1
        assert captured_cancel_kwargs.get("exchange_index") == 3
        assert captured_cancel_kwargs.get("ticker") == "NBA-TEST"


# ---------------------------------------------------------------------------
# 5. MarketMaker ensure_shard_collateral
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_market_maker_ensure_shard_collateral_shard0_noop():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=0):
        bot = AvellanedaStoikovBot("NFL-GAME-1")
        assert bot.exchange_index == 0
        # Shard 0 requires no intra-exchange rebalance
        res = await bot.ensure_shard_collateral(15.0)
        assert res is True


@pytest.mark.asyncio
async def test_market_maker_ensure_shard_collateral_shard3_rebalance_trigger():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        assert bot.exchange_index == 3

        # Shard 3 has $5.00 initially (needs $10.00 transfer), then reports $15.00 on verification poll
        bot.om.get_portfolio_balances = MagicMock(side_effect=[
            {"total_balance_dollars": 30.0, "shard_balances": {0: 25.0, 3: 5.0}},
            {"total_balance_dollars": 30.0, "shard_balances": {0: 15.0, 3: 15.0}},
        ])
        bot.om.transfer_intra_exchange = MagicMock(return_value=True)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            res = await bot.ensure_shard_collateral(min_required_dollars=15.0)
            assert res is True
            bot.om.transfer_intra_exchange.assert_called_once_with(
                source_shard=0,
                destination_shard=3,
                amount_dollars=10.0,
            )


@pytest.mark.asyncio
async def test_market_maker_ensure_shard_collateral_shard3_settlement_timeout():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        # Shard 3 remains underfunded across all verification poll attempts
        bot.om.get_portfolio_balances = MagicMock(return_value={
            "total_balance_dollars": 30.0,
            "shard_balances": {0: 25.0, 3: 5.0}
        })
        bot.om.transfer_intra_exchange = MagicMock(return_value=True)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            res = await bot.ensure_shard_collateral(min_required_dollars=15.0)
            assert res is False


# ---------------------------------------------------------------------------
# 6. Stop-Loss Hedge Circuit Breaker
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stop_loss_hedge_failure_trips_kill_switch():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        bot.om.place_order = AsyncMock(return_value=None)  # Simulates order rejection/failure
        bot.ensure_shard_collateral = AsyncMock(return_value=False)

        kill_switch_mock = MagicMock()
        kill_switch_mock.trigger_synchronous = MagicMock()

        with patch("execution.kill_switch.KillSwitch", return_value=kill_switch_mock):
            with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as alert_mock:
                # 1st failure (selling YES to hedge long inventory)
                await bot._update_quotes(new_bid=None, new_ask=45)
                assert bot._consecutive_hedge_failures == 1
                assert bot._market_inactive is False

                # 2nd failure
                await bot._update_quotes(new_bid=None, new_ask=45)
                assert bot._consecutive_hedge_failures == 2
                assert bot._market_inactive is False

                # 3rd failure: must trip kill switch and alert!
                await bot._update_quotes(new_bid=None, new_ask=45)
                assert bot._consecutive_hedge_failures == 3
                assert bot._market_inactive is True
                kill_switch_mock.trigger_synchronous.assert_called_once_with(ticker="NBA-GAME-1")
                alert_mock.assert_called_once()
                assert "CRITICAL" in alert_mock.call_args[0][0]


@pytest.mark.asyncio
async def test_stop_loss_hedge_success_resets_failure_counter():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        bot._consecutive_hedge_failures = 2

        bot.om.place_order = AsyncMock(return_value="order-success-123")
        await bot._update_quotes(new_bid=None, new_ask=45)

        assert bot._consecutive_hedge_failures == 0
        assert bot.current_ask_id == "order-success-123"


def test_kill_switch_sync_retries_failed_deletion_in_step_4():
    om = MagicMock()
    om.active_orders = {
        "cid-1": {"kalshi_order_id": "kid-1", "ticker": "NBA-1", "exchange_index": 3}
    }
    killer = KillSwitch(om, ticker="NBA-1")

    # Step 1 fails on kid-1 (returns False), Step 4 retries and succeeds (returns True)
    delete_calls = []
    def fake_delete(kalshi_id, client_id=None, exchange_index=None, ticker=None):
        delete_calls.append(kalshi_id)
        if len(delete_calls) == 1:
            return False  # Failed on initial attempt
        return True  # Succeeded on sweep retry

    killer._delete_order_sync = MagicMock(side_effect=fake_delete)
    killer._fetch_resting_orders_sync = MagicMock(return_value=[
        {"order_id": "kid-1", "client_order_id": "cid-1", "ticker": "NBA-1", "exchange_index": 3}
    ])

    killer.trigger_synchronous(ticker="NBA-1")

    # kid-1 must be called twice: first in step 1, then retried in step 4 because step 1 failed
    assert killer._delete_order_sync.call_count == 2
    assert delete_calls == ["kid-1", "kid-1"]


@pytest.mark.asyncio
async def test_market_maker_hedge_failure_uses_dynamic_collateral_floor():
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("NBA-GAME-1")
        bot.om.place_order = AsyncMock(return_value=None)
        bot.ensure_shard_collateral = AsyncMock(return_value=False)

        # 50 contracts @ 60c = $30.00 notional. Floor should be $30.00 + $5.00 = $35.00
        bot._current_quote_size = 50
        await bot._update_quotes(new_bid=None, new_ask=60)

        bot.ensure_shard_collateral.assert_awaited_once_with(min_required_dollars=35.0)


def test_order_manager_get_portfolio_balances_retries_429_with_backoff():
    """Verify get_portfolio_balances rate-limits and retries with backoff on HTTP 429."""
    om = OrderManager()
    om.rate_limiter = MagicMock()

    resp_429 = MockResponse({"error": "rate limited"}, 429)
    resp_200 = MockResponse({
        "balance_dollars": 14.38,
        "balance_breakdown": [
            {"exchange_index": 0, "balance": 1.39},
            {"exchange_index": 3, "balance": 12.97},
        ]
    }, 200)

    with patch("requests.get", side_effect=[resp_429, resp_200]) as mock_get:
        with patch("time.sleep") as mock_sleep:
            res = om.get_portfolio_balances()

            assert res["total_balance_dollars"] == 14.38
            assert res["shard_balances"][3] == 12.97
            assert mock_get.call_count == 2
            assert om.rate_limiter.acquire_sync.call_count == 2
            mock_sleep.assert_called_once_with(1.0)  # Base delay on attempt 1


def test_order_manager_transfer_intra_exchange_rate_limited():
    """Verify transfer_intra_exchange invokes rate limiter before dispatching transfer."""
    om = OrderManager()
    om.rate_limiter = MagicMock()
    om._post_request = MagicMock(return_value=MockResponse({"status": "complete"}, 201))

    success = om.transfer_intra_exchange(
        source_shard=0,
        destination_shard=3,
        amount_dollars=5.0,
    )

    assert success is True
    om.rate_limiter.acquire_sync.assert_called_once()


@pytest.mark.asyncio
async def test_market_maker_ensure_shard_collateral_dynamic_floor_low_bankroll():
    """Verify ensure_shard_collateral scales floor to account balance when bankroll < $15."""
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("KXNBAGAME-LOW-FUNDS")
        bot.shard0_reserve_dollars = 0.0

        # Total account has $14.38 ($12.97 on Shard 3, $1.39 on Shard 0)
        # min_required_dollars is $15.00, but effective_min should scale to $14.38
        bot.om.get_portfolio_balances = MagicMock(side_effect=[
            {"total_balance_dollars": 14.38, "shard_balances": {0: 1.39, 3: 12.97}},
            {"total_balance_dollars": 14.38, "shard_balances": {0: 0.0, 3: 14.36}},
            {"total_balance_dollars": 14.38, "shard_balances": {0: 0.0, 3: 14.36}},
        ])
        bot.om.transfer_intra_exchange = MagicMock(return_value=True)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            res = await bot.ensure_shard_collateral(min_required_dollars=15.0)

            assert res is True
            # Needed is 14.38 - 12.97 = 1.41. Shard 0 has 1.39, so transfers $1.39
            bot.om.transfer_intra_exchange.assert_called_once_with(
                source_shard=0,
                destination_shard=3,
                amount_dollars=1.39,
            )


@pytest.mark.asyncio
async def test_market_maker_ensure_shard_collateral_proceeds_when_shard0_below_min_transfer():
    """Verify ensure_shard_collateral proceeds when Shard 0 < $1.00 but active shard has collateral."""
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("KXNBAGAME-LOW-SHARD0")
        bot.shard0_reserve_dollars = 0.0

        # Shard 3 has $12.97, Shard 0 has $0.50 (cannot transfer < $1.00)
        bot.om.get_portfolio_balances = MagicMock(return_value={
            "total_balance_dollars": 13.47,
            "shard_balances": {0: 0.50, 3: 12.97}
        })
        bot.om.transfer_intra_exchange = MagicMock()

        res = await bot.ensure_shard_collateral(min_required_dollars=15.0)

        assert res is True
        # No transfer attempted because Shard 0 has < $1.00
        bot.om.transfer_intra_exchange.assert_not_called()


@pytest.mark.asyncio
async def test_market_maker_startup_gating_aborts_on_uncollateralized_shard():
    """Verify that bot startup aborts with RuntimeError when shard collateral fails across all 3 retries."""
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("KXNBAGAME-STARTUP-GATE")
        bot.om.sync_and_recover_state = AsyncMock()
        bot.ws_client.connect = AsyncMock()
        bot.ws_client.is_connected = True
        bot.inv_manager.hydrate = AsyncMock(return_value=True)
        bot.ensure_shard_collateral = AsyncMock(return_value=False)
        bot._refresh_market_metadata = AsyncMock()

        with patch("strategy.market_maker.start_metrics_server"):
            with patch("strategy.market_maker.send_alert", new_callable=AsyncMock) as mock_alert:
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    with pytest.raises(RuntimeError, match="Startup failed: Insufficient verified shard collateral"):
                        await bot.start()

                    assert bot.ensure_shard_collateral.await_count == 3
                    assert bot.running is False
                    mock_alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_market_maker_standard_quote_failure_triggers_background_rebalance():
    """Verify standard BID order placement failure triggers shard rebalance after 60s cooldown."""
    with patch("strategy.market_maker.get_market_exchange_index", return_value=3):
        bot = AvellanedaStoikovBot("KXNBAGAME-REBAL-TRIGGER")
        bot.om.place_order = AsyncMock(return_value=None)
        bot.ensure_shard_collateral = AsyncMock(return_value=True)
        bot._last_shard_rebalance_time = 0.0  # Cooldown elapsed

        with patch("asyncio.create_task") as mock_create_task:
            # Place standard quote (not hedge: new_bid and new_ask both provided)
            await bot._update_quotes(new_bid=40, new_ask=60)

            # Background task must be launched for ensure_shard_collateral on first order failure
            # Second failure on the same tick is suppressed by the 60s cooldown
            assert mock_create_task.call_count == 1

