"""
Primary Production Trading Bot Entrypoint

This script initiates the main Avellaneda-Stoikov market-making loop. 
It selects the target market (or a random high-liquidity market if target is blank), 
wires a manual Ctrl+C SIGINT trigger to clean up resting quotes on termination, 
and launches the websocket and quoting loops.
"""

import asyncio
import signal
import random
import requests
import certifi
import sys


from strategy.market_maker import AvellanedaStoikovBot
from execution.kill_switch import KillSwitch


async def main():
    from config import BASE_URL, TARGET_TICKER, RISK_GAMMA, MIN_SPREAD, ORDER_SIZE
    
    ticker = TARGET_TICKER
    if not ticker:
        print("No TARGET_TICKER in .env, fetching a random active market...")
        r = requests.get(BASE_URL + "/trade-api/v2/markets", params={"limit": 1000}, verify=certifi.where())
        eligible_markets = [m["ticker"] for m in r.json().get("markets", []) if m.get("status") in ("open", "active")]
        
        if not eligible_markets:
            print("No active markets found on Demo.")
            sys.exit(1)
            
        # Prioritize high-liquidity markets like Basketball on Demo
        high_liquidity = [m for m in eligible_markets if "NBA" in m or "NCAA" in m]
        if high_liquidity:
            ticker = random.choice(high_liquidity)
        else:
            ticker = random.choice(eligible_markets)
        
    print(f"Selected Market: {ticker}")
    print("Starting Avellaneda-Stoikov Bot... Press Ctrl+C to Kill.")
    
    # 1. Initialize Bot
    bot = AvellanedaStoikovBot(
        ticker=ticker,
        gamma=RISK_GAMMA,
        min_spread=MIN_SPREAD,
        order_size=ORDER_SIZE
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
