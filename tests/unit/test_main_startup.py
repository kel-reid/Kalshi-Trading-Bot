"""
Unit tests for main.py startup market discovery and idle retry logic.
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from main import main


@pytest.mark.asyncio
async def test_main_startup_retries_discovery_until_market_found():
    """Verify main() idles and retries discovery when initial market discovery returns None."""
    discovery_calls = 0

    async def mock_discover(**kwargs):
        nonlocal discovery_calls
        discovery_calls += 1
        if discovery_calls == 1:
            return None  # First attempt: quiet market
        return "KXNFLGAME-26SEP17DETBUF"  # Second attempt: market found

    mock_bot = MagicMock()
    mock_bot.start = AsyncMock(return_value=None)
    mock_bot.stop = AsyncMock(return_value=True)
    mock_bot.om = MagicMock()

    with patch("utils.market_discovery.discover_active_market_async", side_effect=mock_discover), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch"), \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        await main()

        assert discovery_calls == 2
        mock_sleep.assert_called_once_with(30)
        mock_bot.start.assert_called_once()
        mock_bot.stop.assert_called_once()


@pytest.mark.asyncio
async def test_main_signal_shutdown():
    """Verify SIGINT/SIGTERM triggers synchronous kill and delegates clean shutdown to bot.stop()."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.stop = AsyncMock()

    mock_killer = MagicMock()
    mock_killer.trigger_synchronous = MagicMock()

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    # When bot.start() is invoked, trigger the registered handle_shutdown handler
    async def mock_start():
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        handler(signal.SIGINT, None)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        
        await main()

    mock_killer.trigger_synchronous.assert_called_once_with(ticker="KXNFLGAME-TEST")
    mock_bot.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_sigterm_shutdown():
    """Verify SIGTERM triggers synchronous kill and bot.stop()."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.stop = AsyncMock()

    mock_killer = MagicMock()
    mock_killer.trigger_synchronous = MagicMock()

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    async def mock_start():
        handler = signal_handlers.get(signal.SIGTERM)
        assert handler is not None
        handler(signal.SIGTERM, None)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        
        await main()

    mock_killer.trigger_synchronous.assert_called_once_with(ticker="KXNFLGAME-TEST")
    mock_bot.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_bot_crash_triggers_safety_and_stop():
    """Verify bot crash triggers killer.trigger() and bot.stop()."""
    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.start = AsyncMock(side_effect=RuntimeError("Unexpected MM loop crash"))
    mock_bot.stop = AsyncMock()
    mock_killer = MagicMock()
    mock_killer.trigger = AsyncMock()

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer):
        
        with pytest.raises(RuntimeError, match="Unexpected MM loop crash"):
            await main()

    mock_killer.trigger.assert_awaited_once_with(ticker="KXNFLGAME-TEST")
    mock_bot.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_startup_shutdown_signal_during_discovery():
    """Verify signal during discovery aborts startup cleanly without initializing bot."""
    import signal

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    async def mock_discover(**kwargs):
        # Fire signal during discovery and return valid ticker
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        handler(signal.SIGINT, None)
        return "KXNFLGAME-DISCOVERED"

    mock_bot_cls = MagicMock()

    with patch("utils.market_discovery.discover_active_market_async", side_effect=mock_discover), \
         patch("main.AvellanedaStoikovBot", mock_bot_cls), \
         patch("main.signal.signal", side_effect=mock_signal):
        await main()

    # Bot should never be instantiated or started
    mock_bot_cls.assert_not_called()


@pytest.mark.asyncio
async def test_main_signal_idempotent():
    """Verify multiple signals are handled idempotently without multiple kill triggers."""
    import signal

    mock_bot = MagicMock()
    mock_bot.ticker = "KXNFLGAME-TEST"
    mock_bot.stop = AsyncMock()

    mock_killer = MagicMock()
    mock_killer.trigger_synchronous = MagicMock()

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    async def mock_start():
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        # Fire signal twice
        handler(signal.SIGINT, None)
        handler(signal.SIGINT, None)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass

    mock_bot.start = AsyncMock(side_effect=mock_start)

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        await main()

    # Synchronous kill triggered exactly once despite double signal
    mock_killer.trigger_synchronous.assert_called_once()
    mock_bot.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_signal_during_bot_construction():
    """Verify that a signal arriving during bot construction triggers async kill and aborts before bot.start()."""
    import signal

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    mock_bot = MagicMock()
    mock_bot.start = AsyncMock()
    mock_bot.stop = AsyncMock()
    mock_bot.om = MagicMock()

    mock_killer = MagicMock()
    mock_killer.trigger = AsyncMock()
    mock_killer.trigger_synchronous = MagicMock()

    def construct_bot(**kwargs):
        # Fire signal while constructor is running (killer is still None in main)
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        handler(signal.SIGINT, None)
        return mock_bot

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", side_effect=construct_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        await main()

    # Synchronous kill could not run during construction because killer was None
    mock_killer.trigger_synchronous.assert_not_called()
    # Reconciled after arming kill switch: async kill and stop called
    mock_killer.trigger.assert_awaited_once()
    mock_bot.stop.assert_awaited_once()
    # bot.start() must NEVER be started
    mock_bot.start.assert_not_called()


@pytest.mark.asyncio
async def test_main_shutdown_failure_triggers_sync_kill_and_raises_runtime_error():
    """Verify that if bot.stop() returns False during finally, killer.trigger_synchronous is called and RuntimeError is raised."""
    mock_bot = MagicMock()
    mock_bot.start = AsyncMock(return_value=None)
    mock_bot.stop = AsyncMock(return_value=False)
    mock_killer = MagicMock()
    mock_killer.trigger_synchronous = MagicMock()

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
         patch("main.KillSwitch", return_value=mock_killer):
        with pytest.raises(RuntimeError, match="Shutdown failed: active orders could not be confirmed cancelled"):
            await main()

    mock_bot.stop.assert_awaited_once()
    mock_killer.trigger_synchronous.assert_called_once()


@pytest.mark.asyncio
async def test_main_shutdown_during_init_failure_triggers_sync_kill_and_raises():
    """Verify that if shutdown occurs during bot construction and bot.stop() returns False, synchronous kill is triggered and RuntimeError is raised."""
    import signal

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    mock_bot = MagicMock()
    mock_bot.start = AsyncMock()
    mock_bot.stop = AsyncMock(return_value=False)
    mock_bot.om = MagicMock()

    mock_killer = MagicMock()
    mock_killer.trigger = AsyncMock()
    mock_killer.trigger_synchronous = MagicMock()

    def construct_bot(**kwargs):
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        handler(signal.SIGINT, None)
        return mock_bot

    with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXNFLGAME-TEST"), \
         patch("main.AvellanedaStoikovBot", side_effect=construct_bot), \
         patch("main.KillSwitch", return_value=mock_killer), \
         patch("main.signal.signal", side_effect=mock_signal):
        with pytest.raises(RuntimeError, match="Shutdown failed: active orders could not be confirmed cancelled"):
            await main()

    mock_killer.trigger.assert_awaited_once()
    mock_bot.stop.assert_awaited_once()
    mock_killer.trigger_synchronous.assert_called_once()


@pytest.mark.asyncio
async def test_main_shutdown_interrupts_in_flight_discovery():
    """Verify that a shutdown signal during in-flight market discovery interrupts discovery and aborts startup promptly."""
    import signal

    signal_handlers = {}

    def mock_signal(sig, handler):
        signal_handlers[sig] = handler

    discovery_cancelled = False

    async def in_flight_discover(**kwargs):
        nonlocal discovery_cancelled
        handler = signal_handlers.get(signal.SIGINT)
        assert handler is not None
        handler(signal.SIGINT, None)
        try:
            await asyncio.sleep(100)
            return "KXNFL-NEVER"
        except asyncio.CancelledError:
            discovery_cancelled = True
            raise

    with patch("utils.market_discovery.discover_active_market_async", side_effect=in_flight_discover), \
         patch("main.signal.signal", side_effect=mock_signal):
        await main()

    assert discovery_cancelled is True


def test_main_shutdown_when_signal_received_while_alert_lock_held():
    """
    Verify in a separate subprocess with parent-enforced timeout that main.py shutdown
    cancels orders and completes without deadlock when a signal arrives while _alert_lock is held,
    and assert that _alert_lock is held when shutdown reaches the alert.
    """
    import subprocess
    import sys
    import os

    child_code = """
import asyncio
import signal
import sys
from unittest.mock import MagicMock, AsyncMock, patch

from utils.alerting import _alert_lock, send_alert_sync, reset_alert_history
from execution.kill_switch import KillSwitch
from main import main

reset_alert_history()

mock_om = MagicMock()
mock_om.active_orders = {
    "c-1": {"kalshi_order_id": "k-1", "exchange_index": 0, "ticker": "KXTEST"}
}
killer = KillSwitch(mock_om)
delete_order_calls = []
killer._delete_order_sync = lambda *args, **kwargs: (delete_order_calls.append((args, kwargs)), True)[1]
killer._fetch_resting_orders_sync = lambda *args, **kwargs: []

signal_handlers = {}
def mock_signal(sig, handler):
    signal_handlers[sig] = handler

alert_called = asyncio.Event()
lock_held_when_alert_reached = False

orig_send_alert_sync = send_alert_sync
def wrapping_send_alert_sync(*args, **kwargs):
    global lock_held_when_alert_reached
    if _alert_lock._is_owned():
        lock_held_when_alert_reached = True
    alert_called.set()
    return orig_send_alert_sync(*args, **kwargs)

async def mock_start():
    handler = signal_handlers[signal.SIGINT]
    with _alert_lock:
        handler(signal.SIGINT, None)
        await alert_called.wait()
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        pass

mock_bot = MagicMock()
mock_bot.ticker = "KXTEST"
mock_bot.start = AsyncMock(side_effect=mock_start)
mock_bot.stop = AsyncMock(return_value=True)

with patch("utils.market_discovery.discover_active_market_async", new_callable=AsyncMock, return_value="KXTEST"), \
     patch("main.AvellanedaStoikovBot", return_value=mock_bot), \
     patch("main.KillSwitch", return_value=killer), \
     patch("main.signal.signal", side_effect=mock_signal), \
     patch("utils.alerting.ALERT_WEBHOOK_URL", ""), \
     patch("utils.alerting.send_alert_sync", side_effect=wrapping_send_alert_sync):
    asyncio.run(main())

assert lock_held_when_alert_reached is True, "Lock was not held when alert was reached!"
assert len(delete_order_calls) == 1, "Order deletion was not called!"
print("SUBPROCESS_TEST_SUCCESS")
"""

    env = dict(os.environ)
    env["KALSHI_API_KEY"] = "mock-key"
    env["KALSHI_PRIVATE_KEY"] = "mock-key"

    result = subprocess.run(
        [sys.executable, "-c", child_code],
        env=env,
        timeout=5.0,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Subprocess failed (code {result.returncode}):\n{result.stderr}\n{result.stdout}"
    assert "SUBPROCESS_TEST_SUCCESS" in result.stdout




