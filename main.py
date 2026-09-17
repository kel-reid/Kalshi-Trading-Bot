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
    
    # Wire basic signal handling for graceful exit during startup idle
    shutdown_requested = False
    def startup_shutdown(signum, frame):
        nonlocal shutdown_requested
        print(f"\n\n>>> Signal {signum} received during startup. Exiting cleanly. <<<")
        shutdown_requested = True
        sys.exit(0)

    signal.signal(signal.SIGINT, startup_shutdown)
    signal.signal(signal.SIGTERM, startup_shutdown)

    # 1. Discover Active Market (Idle and retry loop if sports markets are off-hours / quiet)
    retry_interval = 30
    ticker = None
    while not ticker and not shutdown_requested:
        ticker = await discover_active_market_async(target_preference=TARGET_TICKER)
        if not ticker:
            print(
                f"No active in-season sports markets with two-sided quotes currently found on {ENVIRONMENT.capitalize()}. "
                f"Idling and retrying discovery in {retry_interval}s..."
            )
            try:
                await asyncio.sleep(retry_interval)
            except asyncio.CancelledError:
                break

    if not ticker:
        print(f"Startup aborted before an active market was locked in.")
        sys.exit(0)
        
    print(f"Selected Market: {ticker}")
    print("Starting Avellaneda-Stoikov Bot... Press Ctrl+C to Kill.")
    
    # 2. Initialize Bot
    bot = AvellanedaStoikovBot(
        ticker=ticker,
        gamma=RISK_GAMMA,
        min_spread=MIN_SPREAD,
        order_size=ORDER_SIZE,
        target_preference=TARGET_TICKER,
    )
    
    # 2. Wire Safety Kill Switch to manual signals (Ctrl+C and termination signals)
    killer = KillSwitch(bot.om)
    def handle_shutdown(signum, frame):
        print(f"\n\n>>> Signal {signum} received. Safety Kill Switch Triggered <<<")
        # Instantly scrub local execution layer 
        killer.trigger_synchronous()
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    # 3. Start Market Maker Loop
    try:
        await bot.start()
    except Exception as e:
        print(f"Bot crashed: {e}")
        # Always trigger safety on crash
        await killer.trigger()

if __name__ == "__main__":
    asyncio.run(main())
