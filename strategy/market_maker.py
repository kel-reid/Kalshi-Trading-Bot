"""
Avellaneda-Stoikov Market Maker Strategy

This is the main algorithm module that calculates the reservation prices and bid/ask quotes 
for prediction markets. It skews prices according to current inventory risk (long/short YES) 
and places dual-sided quotes around the reservation price to earn the bid-ask spread.
"""

import asyncio
import logging
import time
import math
from typing import Optional

from data.websocket_client import KalshiWebsocketClient
from data.orderbook_manager import OrderbookManager
from data.inventory_manager import InventoryManager
from execution.order_manager import OrderManager
from utils.alerting import send_alert
from utils.metrics import start_metrics_server, BOT_INVENTORY_NET_POSITION, BOT_PNL_CENTS
from config import RISK_GAMMA, MIN_SPREAD, ORDER_SIZE

logger = logging.getLogger("MarketMaker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class AvellanedaStoikovBot:
    """
    Implements a simplified Avellaneda-Stoikov market making algorithm for Kalshi.
    
    Formula:
    R (Reservation Price) = Mid_Price - (Inventory * Risk_Aversion)
    Bid = R - (Spread / 2)
    Ask = R + (Spread / 2)
    """
    def __init__(
        self, 
        ticker: str, 
        gamma: float = None, # Risk aversion. How much 1 contract skews our price (in cents).
        min_spread: int = None, # Minimum spread to quote (in cents).
        order_size: int = None   # Number of contracts to quote on each side.
    ):
        self.ticker = ticker
        self.gamma = gamma if gamma is not None else RISK_GAMMA
        self.min_spread = min_spread if min_spread is not None else MIN_SPREAD
        self.order_size = order_size if order_size is not None else ORDER_SIZE
        
        # Core Managers
        self.ws_client = KalshiWebsocketClient()
        self.ob_manager = OrderbookManager(self.ws_client)
        self.inv_manager = InventoryManager(self.ws_client)
        self.om = OrderManager()
        
        self.running = False
        
        # State tracking for our active quotes
        self.current_bid_id: Optional[str] = None
        self.current_ask_id: Optional[str] = None
        
        self.current_bid_price: Optional[int] = None
        self.current_ask_price: Optional[int] = None

    async def start(self):
        """Initializes infrastructure and starts the main trading loop."""
        logger.info(f"Starting Market Maker for {self.ticker}")
        
        # Start Prometheus Metrics Server
        start_metrics_server(port=8000)

        # 0. Recover state and cancel orphaned orders
        await self.om.sync_and_recover_state()
        
        # 1. Start the WebSocket Connection
        asyncio.create_task(self.ws_client.connect())
        
        # 2. Wait for connection to establish
        while not self.ws_client.is_connected:
            await asyncio.sleep(0.1)
            
        logger.info("WebSocket Connected. Hydrating state...")
        
        # 3. Hydrate initial inventory and subscribe to channels
        await self.inv_manager.hydrate()
        
        # 3b. Launch the periodic reconciliation background task
        asyncio.create_task(self.inv_manager._sync_loop())
        
        await self.inv_manager.subscribe()
        await self.ob_manager.subscribe([self.ticker])
        
        logger.info("State hydrated. Beginning quoting loop.")
        self.running = True
        
        # 4. Enter Main Loop
        try:
            while self.running:
                await self._tick()
                await asyncio.sleep(1) # Re-evaluate every 1 second
        except asyncio.CancelledError:
            logger.info("Market Maker stopped.")
        except Exception as e:
            logger.error(f"Fatal error in trading loop: {e}", exc_info=True)
            await send_alert(f"Fatal error in trading loop for {self.ticker}: {e}")
            self.running = False

    async def _tick(self):
        """The core logic evaluated every cycle."""
        
        # Track PnL and Inventory immediately regardless of orderbook state so metrics always show up on Grafana
        inventory = self.inv_manager.get_position(self.ticker)
        BOT_INVENTORY_NET_POSITION.labels(ticker=self.ticker).set(inventory)
        
        balance = self.inv_manager.get_balance()
        BOT_PNL_CENTS.labels(ticker=self.ticker).set(balance)
        
        best_bid = self.ob_manager.get_best_bid(self.ticker)
        best_ask = self.ob_manager.get_best_ask(self.ticker)
        
        if not best_bid or not best_ask:
            # Orderbook is empty. Normally we withdraw, but for testing (min_spread 0), we force a quote.
            if self.min_spread == 0:
                mid_price = 50.0
            else:
                await self._cancel_all_quotes()
                return
        else:
            bid_price = best_bid[0]
            ask_price = best_ask[0]
            
            # 1. Calculate Mid Price
            mid_price = (bid_price + ask_price) / 2.0
        
        # 2. Get Inventory
        # Convention: positive inventory = holding net YES
        
        # 3. Calculate Reservation Price (R)
        # R = M - (q * gamma)
        reservation_price = mid_price - (inventory * self.gamma)
        
        # 4. Calculate Optimal Bid/Ask
        optimal_bid = math.floor(reservation_price - (self.min_spread / 2.0))
        optimal_ask = math.ceil(reservation_price + (self.min_spread / 2.0))
        
        # Ensure prices fit within Kalshi bounds (1c to 99c)
        optimal_bid = max(1, min(optimal_bid, 98))
        optimal_ask = max(2, min(optimal_ask, 99))
        
        # Ensure we don't cross the spread against ourselves
        if optimal_bid >= optimal_ask:
            optimal_bid = optimal_ask - 1
            
        # Active Inventory Mitigation:
        # If inventory is skewed too far, stop quoting the skew direction and aggressively cross/tighten on the other.
        HEDGE_THRESHOLD = 5
        if inventory >= HEDGE_THRESHOLD:
            logger.warning(f"[HEDGE ACTIVE] Long inventory high ({inventory}). Halting BIDs, crossing ASKs to exit.")
            optimal_bid = None # Do not buy more YES
            if best_bid:
                optimal_ask = max(1, min(best_bid[0], 99)) # Match the best bid to fill immediately
            logger.warning(f"[HEDGE ACTIVE] Short inventory high ({inventory}). Halting ASKs, crossing BIDs to exit.")
            optimal_ask = None # Do not sell more YES
            if best_ask:
                optimal_bid = max(1, min(best_ask[0], 98)) # Match the best ask to fill immediately

        if optimal_bid is not None and optimal_ask is not None:
            logger.info(
                f"[A-S MATH] Mid={mid_price:.1f}c | Inventory={inventory} | Gamma={self.gamma} | "
                f"ReservationPrice={reservation_price:.2f}c | Spread={self.min_spread}c "
                f"→ Bid={optimal_bid}c  Ask={optimal_ask}c"
            )
        else:
            logger.info(
                f"[A-S MATH] Mid={mid_price:.1f}c | Inventory={inventory} | "
                f"→ Bid={optimal_bid}c  Ask={optimal_ask}c (Hedged)"
            )
            
        # 5. Execute Output
        await self._update_quotes(optimal_bid, optimal_ask)

    async def _update_quotes(self, new_bid: Optional[int], new_ask: Optional[int]):
        """Places or replaces quotes if the optimal prices have shifted."""
        cancel_tasks = []
        cancel_bid = False
        cancel_ask = False

        # Handle BID Side
        if new_bid != self.current_bid_price:
            if self.current_bid_id:
                cancel_tasks.append(self.om.cancel_order(self.current_bid_id))
                cancel_bid = True

        # Handle ASK Side (Selling YES contracts)
        if new_ask != self.current_ask_price:
            if self.current_ask_id:
                cancel_tasks.append(self.om.cancel_order(self.current_ask_id))
                cancel_ask = True

        # Execute cancellations before clearing IDs so that IDs are only cleared on success
        if cancel_tasks:
            results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
            result_iter = iter(results)
            if cancel_bid:
                result = next(result_iter)
                if not isinstance(result, Exception):
                    self.current_bid_id = None
                    self.current_bid_price = None
            if cancel_ask:
                result = next(result_iter)
                if not isinstance(result, Exception):
                    self.current_ask_id = None
                    self.current_ask_price = None

        # Place new BID if needed
        if new_bid != self.current_bid_price:
            if new_bid is not None and self.current_bid_id is None:
                logger.info(f">> Placing new BID: {self.order_size} YES @ {new_bid}c")
                self.current_bid_id = await self.om.place_order(
                    ticker=self.ticker, side="yes", action="buy", count=self.order_size, price=new_bid
                )
                self.current_bid_price = new_bid if self.current_bid_id else None

        # Place new ASK if needed
        if new_ask != self.current_ask_price:
            if new_ask is not None and self.current_ask_id is None:
                logger.info(f">> Placing new ASK: {self.order_size} YES @ {new_ask}c")
                self.current_ask_id = await self.om.place_order(
                    ticker=self.ticker, side="yes", action="sell", count=self.order_size, price=new_ask
                )
                self.current_ask_price = new_ask if self.current_ask_id else None

    async def _cancel_all_quotes(self):
        """Withdraws all active quotes from the market."""
        tasks = []
        if self.current_bid_id:
            tasks.append(self.om.cancel_order(self.current_bid_id))
            self.current_bid_id = None
            self.current_bid_price = None
        if self.current_ask_id:
            tasks.append(self.om.cancel_order(self.current_ask_id))
            self.current_ask_id = None
            self.current_ask_price = None
            
        if tasks:
            logger.info("Withdrawing quotes...")
            await asyncio.gather(*tasks, return_exceptions=True)
            
    async def stop(self):
        self.running = False
        await self._cancel_all_quotes()
