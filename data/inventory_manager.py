"""
Inventory and Balance Manager

This module tracks USD balance and contract positions in real-time. It initialises 
the state via REST API hydration and maintains it by listening to WebSocket fill events. 
It also runs a background reconciliation loop to prevent state drift.
"""

import logging
import requests
import asyncio
import certifi
from typing import Dict, Any

# Provide required imports from our new module

from config import BASE_URL
from auth.kalshi_auth import get_auth_headers
from utils.metrics import measure_latency

logger = logging.getLogger("InventoryManager")

class InventoryManager:
    """
    Maintains real-time tracking of current position sizes and USD balance.
    Hydrates initial state via REST and updates via WebSocket 'fill' events.
    """
    def __init__(self, ws_client):
        self.ws_client = ws_client
        self.balance_cents: int = 0
        # self.positions[ticker] = position_size (positive means net 'yes', negative means net 'no' or just track absolute shares)
        # Kalshi usually tracks 'position' as an integer of contracts.
        self.positions: Dict[str, int] = {}
        
        self.ws_client.add_message_handler(self._handle_message)

    def _hydrate_balance(self):
        """Fetch initial balance via REST."""
        sign_path = "/trade-api/v2/portfolio/balance"
        headers = get_auth_headers(method="GET", sign_path=sign_path)
        
        with measure_latency("GET", "/trade-api/v2/portfolio/balance"):
            response = requests.get(BASE_URL + sign_path, headers=headers, timeout=10, verify=certifi.where())
            
        if response.status_code == 200:
            self.balance_cents = response.json().get("balance", 0)
            logger.info(f"Hydrated Balance: {self.balance_cents} cents.")
        else:
            logger.error(f"Failed to fetch balance: {response.text}")

    def _hydrate_positions(self):
        """Fetch initial positions via REST."""
        sign_path = "/trade-api/v2/portfolio/positions"
        headers = get_auth_headers(method="GET", sign_path=sign_path)
        
        with measure_latency("GET", "/trade-api/v2/portfolio/positions"):
            response = requests.get(BASE_URL + sign_path, headers=headers, params={"limit": 200}, timeout=10, verify=certifi.where())
            
        if response.status_code == 200:
            market_positions = response.json().get("market_positions", [])
            new_positions = {}
            for pos in market_positions:
                ticker = pos.get("ticker")
                # Kalshi V2 API returns position as 'position_fp' (float string) or 'position' (int)
                position = int(float(pos.get("position_fp", pos.get("position", 0))))
                if ticker and position != 0:
                    new_positions[ticker] = position
            
            self.positions = new_positions
            logger.info(f"Hydrated {len(self.positions)} active positions: {self.positions}")
        else:
            logger.error(f"Failed to fetch positions: {response.text}")

    async def hydrate(self):
        """Run hydration asynchronously to avoid blocking the event loop."""
        logger.info("Hydrating inventory state from REST API...")
        await asyncio.to_thread(self._hydrate_balance)
        await asyncio.to_thread(self._hydrate_positions)
        
    async def _sync_loop(self):
        """
        Continuously runs in the background. Every 5 minutes, 
        fetches the authoritative ground truth from the REST API 
        to correct any state drift from missed WebSocket messages.
        """
        logger.info("Starting background state drift reconciliation loop (5 min intervals).")
        while True:
            await asyncio.sleep(300) # 5 minutes
            try:
                logger.info("Running periodic inventory reconciliation to fix state drift...")
                await self.hydrate()
            except Exception as e:
                logger.error(f"Error during periodic inventory reconciliation: {e}")

    async def subscribe(self):
        """Subscribe to the 'fill' channel to listen for my own execution updates."""
        await self.ws_client.subscribe(["fill"])

    async def _handle_message(self, message: Dict[str, Any]):
        msg_type = message.get("type")
        
        if msg_type == "fill":
            self._handle_fill(message.get("msg", {}))

    def _handle_fill(self, fill_msg: Dict[str, Any]):
        """
        Process a fill notification and update inventory.
        Typically on Kalshi a fill might look like:
        {
            "market_ticker": "KXINFL-23...",
            "action": "buy",
            "side": "yes",
            "count": 10,
            "price": 45
        }
        """
        ticker = fill_msg.get("market_ticker")
        action = fill_msg.get("action") # buy or sell
        side = fill_msg.get("side") # yes or no
        count = fill_msg.get("count", 0)
        price = fill_msg.get("price", 0) # in cents
        
        if not ticker or count == 0:
            return
            
        # Update balance
        # If we buy, we spend (price * count) cents
        # If we sell, we receive (price * count) cents
        # Wait, selling "yes" might also include resolving risks or just earning the premium.
        # But for simple inventory tracking, we just track the position delta.
        if action == "buy":
            self.balance_cents -= (price * count)
        elif action == "sell":
            self.balance_cents += (price * count)
            
        # Update positions
        # Standard convention: + for 'yes' shares, - for 'no' shares (or tracked separately)
        # Kalshi usually tracks them as positive positions of the specific side.
        # Assuming our simple market maker focuses on 'yes' contracts (or tracks them neutrally as 'yes' equivalents):
        # Let's track the actual balance of shares as reported by Kalshi portfolio (usually positive integer).
        # We will assume position is net 'yes' shares where + is YES and - is NO.
        delta = count if action == "buy" else -count
        
        if side == "no":
            delta = -delta # buying NO is equivalent to selling YES from a risk perspective
            
        current_pos = self.positions.get(ticker, 0)
        self.positions[ticker] = current_pos + delta
        
        logger.info(f"Fill processed for {ticker}: {action} {count} {side} @ {price}c. New Net Pos: {self.positions[ticker]}.")

    def get_position(self, ticker: str) -> int:
        return self.positions.get(ticker, 0)
        
    def get_balance(self) -> int:
        return self.balance_cents
