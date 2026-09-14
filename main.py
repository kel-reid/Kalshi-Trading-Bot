"""
Primary Production Trading Bot Entrypoint

This script initiates the main Avellaneda-Stoikov market-making loop. 
It selects the target market (or a random high-liquidity market if target is blank), 
wires a manual Ctrl+C SIGINT trigger to clean up resting quotes on termination, 
and launches the websocket and quoting loops.
"""

import asyncio
import signal
import sys


from strategy.market_maker import AvellanedaStoikovBot
from execution.kill_switch import KillSwitch


async def main():
    from config import ENVIRONMENT, TARGET_TICKER, RISK_GAMMA, MIN_SPREAD, ORDER_SIZE
    from utils.market_discovery import discover_active_market_async
    
    ticker = await discover_active_market_async(target_preference=TARGET_TICKER)
    if not ticker:
        print(f"No active tradeable markets found on {ENVIRONMENT.capitalize()}.")
        sys.exit(1)
        
    print(f"Selected Market: {ticker}")
    print("Starting Avellaneda-Stoikov Bot... Press Ctrl+C to Kill.")
    
    # 1. Initialize Bot
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
