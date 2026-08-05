"""
Kill Switch Integration Test

This test verifies the safety kill switch mechanism. It places a dummy 1-cent order on 
an active market, verifies that the order is tracked locally, and then triggers the 
asynchronous kill switch. It asserts that the order manager has successfully cleaned 
up and cancelled all resting quotes.
"""

import asyncio
import signal
import json


from execution.order_manager import OrderManager
from execution.kill_switch import KillSwitch

import sys

async def test_kill_switch():
    print("Initializing components...")
    om = OrderManager()
    killer = KillSwitch(om)
    
    # 1. Fetch an active market to ensure we have a valid environment
    print("Fetching an active market ticker for testing...")
    import requests
    import certifi
    from config import BASE_URL
    
    r = requests.get(BASE_URL + "/trade-api/v2/markets", params={"limit": 100}, verify=certifi.where())
    active_markets = [m["ticker"] for m in r.json().get("markets", []) if m.get("status") in ("open", "active")]
    
    assert len(active_markets) > 0, "No active markets found on Demo. Test cannot proceed."
        
    import random
    ticker = random.choice(active_markets)
    
    # 2. Add an order that won't execute immediately (1 cent YES)
    print(f"Placing dummy order on {ticker}...")
    order_id_1 = await om.place_order(ticker, side="yes", action="buy", count=1, price=1)
    
    assert order_id_1 is not None, "Failed to place dummy order. Test aborted."

    print(f"Order successfully placed: {order_id_1}")
    print(f"Active orders before Kill Switch: {len(om.active_orders)}")
    assert len(om.active_orders) >= 1, "Expected at least 1 active order before kill switch trigger."
    
    print("\nSimulating unexpected failure, invoking async Kill Switch...")
    # 3. Trigger the asynchronous kill switch
    await killer.trigger()
    
    print(f"Active orders after Kill Switch: {len(om.active_orders)}")
    assert len(om.active_orders) == 0, "Expected 0 active orders after kill switch trigger."

if __name__ == "__main__":
    om = OrderManager()
    killer = KillSwitch(om)
    
    # Setup Signal Handler for Manual Trigger (Ctrl+C)
    def handle_sigint(signum, frame):
        print("\n\n>>> SIGINT RECEIVED. Manual Kill Switch Triggered <<<")
        killer.trigger_synchronous()
        sys.exit(0)
        
    signal.signal(signal.SIGINT, handle_sigint)
    print("Kill Switch armed. Press Ctrl+C to test manual trigger.")
    asyncio.run(test_kill_switch())
