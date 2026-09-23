"""
Primary Production Trading Bot Entrypoint

This script initiates the main Avellaneda-Stoikov market-making loop. 
It selects the target market (or discovers the most liquid in-season sports market
via SportsSeasonRouter if target is blank), retries with backoff if no markets
have active quotes, wires a manual Ctrl+C SIGINT trigger to clean up resting
quotes on termination, and launches the websocket and quoting loops.
"""

import asyncio
import signal
import sys


from strategy.market_maker import AvellanedaStoikovBot
from execution.kill_switch import KillSwitch


async def main():
    from config import ENVIRONMENT, TARGET_TICKER, RISK_GAMMA, MIN_SPREAD, ORDER_SIZE
    from utils.market_discovery import discover_active_market_async
    
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    killer = None
    shutdown_in_progress = False
    sync_kill_executed = False

    def handle_shutdown(signum, frame=None):
        nonlocal shutdown_in_progress, sync_kill_executed
        if shutdown_in_progress:
            return
        shutdown_in_progress = True
        print(f"\n\n>>> Signal {signum} received. Initiating graceful shutdown... <<<")
        if killer is not None:
            killer.trigger_synchronous()
            sync_kill_executed = True
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # 1. Discover Active Market (Idle and retry loop if sports markets are off-hours / quiet)
    retry_interval = 30
    ticker = None
    while not ticker and not shutdown_event.is_set():
        ticker = await discover_active_market_async(target_preference=TARGET_TICKER)
        if not ticker:
            print(
                f"No active in-season sports markets with two-sided quotes currently found on {ENVIRONMENT.capitalize()}. "
                f"Idling and retrying discovery in {retry_interval}s..."
            )
            try:
                sleep_task = asyncio.create_task(asyncio.sleep(retry_interval))
                wait_task = asyncio.create_task(shutdown_event.wait())
                done, pending = await asyncio.wait(
                    [sleep_task, wait_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                if wait_task in done:
                    break
            except asyncio.CancelledError:
                break

    if not ticker or shutdown_event.is_set():
        print("Startup aborted before an active market was locked in.")
        return
        
    print(f"Selected Market: {ticker}")
    print("Starting Avellaneda-Stoikov Bot... Press Ctrl+C to Kill.")
    
    # 2. Initialize Bot and Wire Kill Switch
    bot = AvellanedaStoikovBot(
        ticker=ticker,
        gamma=RISK_GAMMA,
        min_spread=MIN_SPREAD,
        order_size=ORDER_SIZE,
        target_preference=TARGET_TICKER,
    )
    killer = KillSwitch(bot.om)

    # Reconcile pending-shutdown state if a signal arrived during bot construction
    if shutdown_event.is_set():
        print("Shutdown requested during bot initialization; cleaning up resting quotes and aborting startup.")
        if not sync_kill_executed:
            await killer.trigger()
        shutdown_clean = await bot.stop()
        if not shutdown_clean:
            killer.trigger_synchronous()
            raise RuntimeError("Shutdown failed: active orders could not be confirmed cancelled on exchange.")
        return

    # 3. Start Market Maker Loop with cancellation coordination
    bot_task = asyncio.create_task(bot.start())
    stop_waiter = asyncio.create_task(shutdown_event.wait())

    try:
        done, pending = await asyncio.wait(
            [bot_task, stop_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if bot_task in done:
            exc = bot_task.exception()
            if exc:
                print(f"Bot crashed: {exc}")
                await killer.trigger()
        else:
            bot.running = False
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
    except Exception as e:
        print(f"Bot crashed: {e}")
        await killer.trigger()
    finally:
        stop_waiter.cancel()
        shutdown_clean = await bot.stop()
        if not shutdown_clean:
            killer.trigger_synchronous()
            raise RuntimeError("Shutdown failed: active orders could not be confirmed cancelled on exchange.")

if __name__ == "__main__":
    asyncio.run(main())
