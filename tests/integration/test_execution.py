"""
Order Lifecycle Integration Test

This test verifies basic REST order placements and cancellations. It fetches a list of 
active markets, selects a random market, places a 1-cent limit order (to prevent 
accidental fills), waits 3 seconds, and then cancels the order to verify the full 
execution lifecycle.
"""

import asyncio
import os

from execution.order_manager import OrderManager

async def test_order_lifecycle():
    om = OrderManager()
    
    # We will attempt to place an order deep out of the money to ensure it doesn't get instantly filled.
    print("Fetching an active market ticker for testing...")
    import requests
    from config import BASE_URL
    import certifi
    
    r = requests.get(BASE_URL + "/trade-api/v2/markets", params={"limit": 100}, verify=certifi.where())
    active_markets = [m["ticker"] for m in r.json().get("markets", []) if m.get("status") in ("open", "active")]
    
    if not active_markets:
        print("No active markets found on Demo. Test cannot proceed.")
        return
        
    import random
    ticker = random.choice(active_markets)
    side = "yes"
    action = "buy"
    count = 1
    price = 1 # 1 cent limit price, very unlikely to instantly cross the spread
    
    print(f"Attempting to place test order for {ticker}...")
    client_order_id = await om.place_order(ticker, side, action, count, price)
    
    if client_order_id:
        print(f"Order successfully placed with local ID: {client_order_id}")
        
        # Wait a moment to ensure it hits the orderbook before canceling
        print("Waiting 3 seconds...")
        await asyncio.sleep(3)
        
        print("Attempting to cancel the order...")
        success = await om.cancel_order(client_order_id)
        
        if success:
            print("Order successfully cancelled!")
        else:
            print("Failed to cancel the order. It might have filled or there was an error.")
    else:
        print("Order placement failed.")

if __name__ == "__main__":
    asyncio.run(test_order_lifecycle())
