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
    from config import BASE_URL, ENVIRONMENT, TARGET_TICKER, RISK_GAMMA, MIN_SPREAD, ORDER_SIZE
    
    ticker = TARGET_TICKER.strip() if TARGET_TICKER else ""
    sports_keywords = ("NFL", "MLB", "NBA", "NHL", "EPL", "SOCCER", "NCAA")
    fallback_keywords = ("INX", "SPX", "NASDAQ", "NDX", "BTC", "ETH")

    # Fetch active markets from Kalshi
    r = requests.get(BASE_URL + "/trade-api/v2/markets", params={"limit": 1000}, verify=certifi.where())
    eligible_markets = [m["ticker"] for m in r.json().get("markets", []) if m.get("status") in ("open", "active")]
    
    if not eligible_markets:
        print(f"No active markets found on {ENVIRONMENT.capitalize()}.")
        sys.exit(1)
        
    # Exclude internal composite / shard combo markets
    tradeable_markets = [m for m in eligible_markets if not m.upper().startswith("KXMVE")]

    # 1. Exact match if ticker specified and actively tradeable
    if ticker and ticker in tradeable_markets:
        print(f"Targeting specified market: {ticker}")
    # 2. Keyword / category match if TARGET_TICKER provided (e.g. 'NFL', 'SPORTS', 'MLB')
    elif ticker:
        if ticker.upper() in ("SPORTS", "SPORT", "MAJOR SPORTS"):
            matched = [m for m in tradeable_markets if any(k in m.upper() for k in sports_keywords)]
        else:
            # Match specific sport prefix or keyword (e.g. 'NFL')
            matched = [m for m in tradeable_markets if ticker.upper() in m.upper()]
        
        if matched:
            ticker = random.choice(matched)
            print(f"Matched active market for keyword '{TARGET_TICKER}': {ticker}")
        else:
            print(f"No active markets found matching '{TARGET_TICKER}', falling back to available Major Sports...")
            sports_markets = [m for m in tradeable_markets if any(k in m.upper() for k in sports_keywords)]
            if sports_markets:
                ticker = random.choice(sports_markets)
                print(f"Selected alternative Major Sports market: {ticker}")
            else:
                ticker = random.choice(tradeable_markets)
    # 3. Default: Prioritize NFL / Major Sports markets first
    else:
        sports_markets = [m for m in tradeable_markets if any(k in m.upper() for k in sports_keywords)]
        if sports_markets:
            ticker = random.choice(sports_markets)
            print(f"Auto-selected active Major Sports market: {ticker}")
        else:
            high_liquidity = [m for m in tradeable_markets if any(k in m.upper() for k in fallback_keywords)]
            ticker = random.choice(high_liquidity) if high_liquidity else random.choice(tradeable_markets)
            print(f"No sports markets active currently. Auto-selected market: {ticker}")
        
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
