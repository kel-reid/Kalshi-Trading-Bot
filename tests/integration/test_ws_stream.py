"""
WebSocket Stream Integration Test

This test verifies the WebSocket client connection, channel subscription (orderbook delta), 
and portfolio/inventory data hydration. It connects to the Kalshi exchange, subscribes 
to a random active market, and asserts that best bid/ask updates are received within 
a 5-second window.
"""

import asyncio
import logging
import sys

from data.websocket_client import KalshiWebsocketClient
from data.orderbook_manager import OrderbookManager
from data.inventory_manager import InventoryManager

async def test_websocket_stream():
    client = KalshiWebsocketClient()
    ob_manager = OrderbookManager(client)
    inv_manager = InventoryManager(client)
    
    # Start the connection loop in the background
    asyncio.create_task(client.connect())
    
    print("Fetching an active market ticker for testing...")
    import requests
    import certifi
    from config import BASE_URL
    
    r = requests.get(BASE_URL + "/trade-api/v2/markets", params={"limit": 100}, verify=certifi.where())
    active_markets = [m["ticker"] for m in r.json().get("markets", []) if m.get("status") in ("open", "active")]
    
    assert len(active_markets) > 0, "No active markets found on Demo. Test cannot proceed."
        
    import random
    ticker = random.choice(active_markets)
    print(f"Sending subscription for {ticker}...")
    await ob_manager.subscribe([ticker])
    
    print("Hydrating initial portfolio balance and positions...")
    await inv_manager.hydrate()
    balance = inv_manager.get_balance()
    print(f"Hydrated Balance is: ${balance / 100:.2f}")
    assert balance >= 0, "Portfolio balance hydration failed or is negative."
    
    # Subscribe to fills
    print("Subscribing to fill channel...")
    await inv_manager.subscribe()
    
    # Let it run for 5 seconds to gather data
    has_bid = False
    has_ask = False
    for i in range(5):
        await asyncio.sleep(1)
        best_bid = ob_manager.get_best_bid(ticker)
        best_ask = ob_manager.get_best_ask(ticker)
        pos = inv_manager.get_position(ticker)
        print(f"[{i+1}s] Best Bid: {best_bid} | Best Ask: {best_ask} | Pos: {pos}")
        if best_bid is not None:
            has_bid = True
        if best_ask is not None:
            has_ask = True
            
    print("Done testing.")
    assert has_bid or has_ask, "Did not receive any bid/ask snapshots from WebSocket."

if __name__ == "__main__":
    asyncio.run(test_websocket_stream())
