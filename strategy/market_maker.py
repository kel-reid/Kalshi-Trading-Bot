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
from typing import Optional, Union

from data.websocket_client import KalshiWebsocketClient
from data.orderbook_manager import OrderbookManager
from data.inventory_manager import InventoryManager
from execution.order_manager import OrderManager
from utils.alerting import send_alert
from utils.market_discovery import discover_active_market_async, is_market_active_async
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
        order_size: int = None,  # Number of contracts to quote on each side.
        target_preference: Optional[str] = None,
        auto_rotate: bool = True,
        starvation_timeout: float = 900.0, # 15 minutes
    ):
        self.ticker = ticker
        self.gamma = gamma if gamma is not None else RISK_GAMMA
        self.min_spread = min_spread if min_spread is not None else MIN_SPREAD
        self.order_size = order_size if order_size is not None else ORDER_SIZE
        self.target_preference = target_preference if target_preference is not None else ticker
        self.auto_rotate = auto_rotate
        self.starvation_timeout = starvation_timeout
        
        # Core Managers
        self.ws_client = KalshiWebsocketClient()
        self.ob_manager = OrderbookManager(self.ws_client)
        self.inv_manager = InventoryManager(self.ws_client)
        self.om = OrderManager()
        
        self.running = False
        
        # State tracking for our active quotes
        self.current_bid_id: Optional[str] = None
        self.current_ask_id: Optional[str] = None
        
        self.current_bid_price: Optional[Union[int, float]] = None
        self.current_ask_price: Optional[Union[int, float]] = None
        self._last_empty_ob_log: float = 0.0

        # Starvation tracking
        self._starvation_start_time: Optional[float] = None
        self._starvation_alert_sent: bool = False

        # Periodic market settlement/status check (every 60s)
        self._last_market_status_check: float = time.time()
        self._market_status_check_interval: float = 60.0
        self._market_inactive: bool = False
        self._last_inactive_retry: float = 0.0
        self._inactive_retry_interval: float = 5.0

        # Periodic PnL snapshot tracking
        self._last_pnl_snapshot: float = time.time()
        self._pnl_snapshot_interval: float = 60.0

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
        now = time.time()

        # Track PnL and Inventory immediately regardless of orderbook state so metrics always show up on Grafana
        inventory = self.inv_manager.get_position(self.ticker)
        BOT_INVENTORY_NET_POSITION.labels(ticker=self.ticker).set(inventory)
        
        balance = self.inv_manager.get_balance()
        BOT_PNL_CENTS.labels(ticker=self.ticker).set(balance)

        # Periodic PnL snapshot persistence to PostgreSQL (every 60s)
        if now - self._last_pnl_snapshot >= self._pnl_snapshot_interval:
            self._last_pnl_snapshot = now
            pnl_summary = self.inv_manager.get_pnl_summary(self.ticker)
            asyncio.create_task(
                self.om.record_pnl_snapshot_async(
                    ticker=self.ticker,
                    realized_pnl_cents=pnl_summary.get("realized_pnl_cents", 0.0),
                    unrealized_pnl_cents=pnl_summary.get("unrealized_pnl_cents", 0.0),
                    total_fees_cents=pnl_summary.get("total_fees_cents", 0.0),
                    inventory=inventory,
                    rotation_session_id=pnl_summary.get("rotation_session_id", "")
                )
            )

        # 0. Active Inactive Market Recovery: Retry unconfirmed cancellations and retry finding replacement
        if self._market_inactive:
            if self.current_bid_id or self.current_ask_id:
                logger.warning(f"Retrying quote cancellation for inactive market {self.ticker}...")
                await self._cancel_all_quotes()

            if self.auto_rotate and (now - self._last_inactive_retry >= self._inactive_retry_interval):
                self._last_inactive_retry = now
                logger.info(f"Retrying market discovery for inactive market {self.ticker}...")
                replacement = await discover_active_market_async(
                    target_preference=self.target_preference,
                    exclude_tickers=[self.ticker]
                )
                if replacement:
                    if await self.rotate_market(replacement):
                        self._market_inactive = False
                        logger.info(f"Successfully rotated from inactive market to {replacement}.")
                        return
                    else:
                        logger.warning(f"Rotation to {replacement} aborted; will retry in {self._inactive_retry_interval}s.")
            return

        # Periodic market settlement and expiry detection
        if self.auto_rotate and (now - self._last_market_status_check >= self._market_status_check_interval):
            self._last_market_status_check = now
            is_active = await is_market_active_async(self.ticker)
            if is_active is False:
                logger.warning(f"Market {self.ticker} is no longer active (settled or expired). Initiating rotation...")
                replacement = await discover_active_market_async(
                    target_preference=self.target_preference,
                    exclude_tickers=[self.ticker]
                )
                if replacement:
                    if await self.rotate_market(replacement):
                        self._market_inactive = False
                    else:
                        self._market_inactive = True
                        self._last_inactive_retry = now
                    return
                else:
                    logger.error(f"No replacement market found for {self.ticker}.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
                    await self._cancel_all_quotes()
                    return
            elif is_active is True:
                self._market_inactive = False
            else:
                logger.warning(f"Unable to confirm market status for {self.ticker}; retaining current market.")
        
        best_bid = self.ob_manager.get_best_bid(self.ticker)
        best_ask = self.ob_manager.get_best_ask(self.ticker)
        
        if not best_bid or not best_ask:
            # Orderbook is empty. Normally we withdraw, but for testing (min_spread 0), we force a quote.
            if self.min_spread == 0:
                mid_price = 50.0
            else:
                if self._starvation_start_time is None:
                    self._starvation_start_time = now
                
                starved_duration = now - self._starvation_start_time
                if starved_duration >= self.starvation_timeout and not self._starvation_alert_sent:
                    self._starvation_alert_sent = True
                    msg = (
                        f"[STARVATION ALERT] Orderbook for {self.ticker} has had no two-sided quotes "
                        f"for {int(starved_duration / 60)} minutes. Quoting halted."
                    )
                    logger.error(msg)
                    await send_alert(msg)

                    # Trigger dynamic market rotation if auto_rotate is enabled
                    if self.auto_rotate:
                        logger.warning(
                            f"Orderbook for {self.ticker} has been starved for {int(starved_duration / 60)}m. "
                            f"Initiating auto-rotation to find an active market..."
                        )
                        replacement = await discover_active_market_async(
                            target_preference=self.target_preference,
                            exclude_tickers=[self.ticker]
                        )
                        if replacement:
                            if await self.rotate_market(replacement):
                                self._market_inactive = False
                            else:
                                self._market_inactive = True
                                self._last_inactive_retry = now
                            return
                        else:
                            logger.error(f"No replacement market found for starved market {self.ticker}.")
                            self._market_inactive = True
                            self._last_inactive_retry = now
                            await self._cancel_all_quotes()
                            return

                if now - self._last_empty_ob_log > 30:
                    logger.warning(
                        f"Orderbook for {self.ticker} has no two-sided quotes "
                        f"(best_bid={best_bid}, best_ask={best_ask}, starved={int(starved_duration)}s). "
                        f"Waiting for market activity..."
                    )
                    self._last_empty_ob_log = now
                await self._cancel_all_quotes()
                return
        else:
            if self._starvation_alert_sent:
                logger.info(f"Orderbook liquidity restored for {self.ticker}.")
                await send_alert(f"Orderbook Restored: Two-sided liquidity detected for {self.ticker}.")
            self._starvation_start_time = None
            self._starvation_alert_sent = False

            bid_price = best_bid[0]
            ask_price = best_ask[0]
            
            # 1. Calculate Mid Price
            mid_price = (bid_price + ask_price) / 2.0
        
        # 2. Mark open inventory to market against current mid price
        self.inv_manager.update_orderbook_mid(self.ticker, mid_price)

        # 3. Get Inventory
        # Convention: positive inventory = holding net YES
        
        # 4. Calculate Reservation Price (R)
        # R = M - (q * gamma)
        reservation_price = mid_price - (inventory * self.gamma)
        
        # 5. Calculate Optimal Bid/Ask
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
                optimal_ask = max(2, min(best_bid[0], 99)) # Match the best bid to fill immediately
        elif inventory <= -HEDGE_THRESHOLD:
            logger.warning(f"[HEDGE ACTIVE] Short inventory high ({inventory}). Halting ASKs, crossing BIDs to exit.")
            optimal_ask = None # Do not sell more YES
            if best_ask:
                optimal_bid = max(1, min(best_ask[0], 98)) # Match the best ask to fill immediately

        realized_pnl = self.inv_manager.get_realized_pnl(self.ticker)
        unrealized_pnl = self.inv_manager.get_unrealized_pnl(self.ticker)
        if optimal_bid is not None and optimal_ask is not None:
            logger.info(
                f"[A-S MATH] Mid={mid_price:.1f}c | Inventory={inventory} | Gamma={self.gamma} | "
                f"ReservationPrice={reservation_price:.2f}c | Spread={self.min_spread}c "
                f"→ Bid={optimal_bid}c  Ask={optimal_ask}c | "
                f"Realized={realized_pnl:+.1f}c | Unrealized={unrealized_pnl:+.1f}c"
            )
        else:
            logger.info(
                f"[A-S MATH] Mid={mid_price:.1f}c | Inventory={inventory} | "
                f"→ Bid={optimal_bid}c  Ask={optimal_ask}c (Hedged) | "
                f"Realized={realized_pnl:+.1f}c | Unrealized={unrealized_pnl:+.1f}c"
            )
            
        # 5. Execute Output
        await self._update_quotes(optimal_bid, optimal_ask)

    async def _update_quotes(self, new_bid: Optional[Union[int, float]], new_ask: Optional[Union[int, float]]):
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

    async def _cancel_all_quotes(self) -> bool:
        """Withdraws all active quotes from the market."""
        tasks = []
        cancel_targets = []
        if self.current_bid_id:
            tasks.append(self.om.cancel_order(self.current_bid_id))
            cancel_targets.append(("bid", self.current_bid_id))
        if self.current_ask_id:
            tasks.append(self.om.cancel_order(self.current_ask_id))
            cancel_targets.append(("ask", self.current_ask_id))
             
        if not tasks:
            return True

        logger.info("Withdrawing quotes...")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_cancelled = True

        for (side, order_id), result in zip(cancel_targets, results):
            if isinstance(result, Exception):
                logger.error(f"Failed to cancel {side} quote {order_id}: {result}")
                all_cancelled = False
                continue
            if result is not True:
                logger.error(f"Failed to cancel {side} quote {order_id}.")
                all_cancelled = False
                continue

            if side == "bid":
                self.current_bid_id = None
                self.current_bid_price = None
            else:
                self.current_ask_id = None
                self.current_ask_price = None

        return all_cancelled

    async def rotate_market(self, new_ticker: str) -> bool:
        """
        Dynamically rotate bot quoting and orderbook subscription to a new market ticker
        without restarting the container process.
        """
        old_ticker = self.ticker
        if new_ticker == old_ticker:
            return True

        logger.warning(f"Rotating target market from {old_ticker} to {new_ticker}...")

        # Persist final PnL attribution snapshot for the market being rotated out
        try:
            pnl_summary = self.inv_manager.get_pnl_summary(old_ticker)
            await self.om.record_pnl_snapshot_async(
                ticker=old_ticker,
                realized_pnl_cents=pnl_summary.get("realized_pnl_cents", 0.0),
                unrealized_pnl_cents=pnl_summary.get("unrealized_pnl_cents", 0.0),
                total_fees_cents=pnl_summary.get("total_fees_cents", 0.0),
                inventory=self.inv_manager.get_position(old_ticker),
                rotation_session_id=pnl_summary.get("rotation_session_id", "")
            )
        except Exception as e:
            logger.error(f"Failed to record rotation PnL snapshot for {old_ticker}: {e}")

        # 1. Withdraw all active quotes on the previous ticker
        if not await self._cancel_all_quotes():
            logger.error(f"Aborting market rotation from {old_ticker} to {new_ticker}; quote cancellation was not confirmed.")
            return False

        # 2. Swap orderbook subscription
        await self.ob_manager.unsubscribe([old_ticker])
        await self.ob_manager.subscribe([new_ticker])

        # 3. Update target ticker and reset state trackers
        self.ticker = new_ticker
        self._starvation_start_time = None
        self._starvation_alert_sent = False
        self._last_empty_ob_log = 0.0
        self._last_market_status_check = time.time()
        self._market_inactive = False

        # Start a new tracking session for the new market ticker
        self.inv_manager.pnl_tracker.reset_market_session(new_ticker)

        logger.info(f"Market rotation complete. Now trading {new_ticker}.")
        await send_alert(f"Market Rotated: Switched target from {old_ticker} to active market {new_ticker}.")
        return True

    async def stop(self):
        self.running = False
        try:
            pnl_summary = self.inv_manager.get_pnl_summary(self.ticker)
            await self.om.record_pnl_snapshot_async(
                ticker=self.ticker,
                realized_pnl_cents=pnl_summary.get("realized_pnl_cents", 0.0),
                unrealized_pnl_cents=pnl_summary.get("unrealized_pnl_cents", 0.0),
                total_fees_cents=pnl_summary.get("total_fees_cents", 0.0),
                inventory=self.inv_manager.get_position(self.ticker),
                rotation_session_id=pnl_summary.get("rotation_session_id", "")
            )
        except Exception as e:
            logger.error(f"Failed to record shutdown PnL snapshot for {self.ticker}: {e}")
        await self._cancel_all_quotes()
