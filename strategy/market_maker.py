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
import os
from typing import Optional, Union

from data.websocket_client import KalshiWebsocketClient
from data.orderbook_manager import OrderbookManager
from data.inventory_manager import InventoryManager
from execution.order_manager import OrderManager
from utils.alerting import send_alert
from utils.market_discovery import discover_active_market_async, is_market_active_async
from utils.metrics import (
    start_metrics_server,
    BOT_INVENTORY_NET_POSITION,
    BOT_PNL_CENTS,
    KALSHI_REALIZED_PNL_CENTS,
    KALSHI_UNREALIZED_PNL_CENTS,
    KALSHI_FEES_PAID_CENTS,
    SAFEGUARD_EVENTS_TOTAL,
)
from config import (
    RISK_GAMMA,
    MIN_SPREAD,
    ORDER_SIZE,
    ORDER_DOLLARS,
    MAX_ORDER_CONTRACTS,
    MAX_HEDGE_INVENTORY,
    MIN_MID_PRICE,
    MAX_MID_PRICE,
    MIN_TIME_TO_CLOSE_SECONDS,
)

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
        order_size: int = None,  # Number of contracts to quote on each side (fallback/fixed mode).
        order_dollars: float = None,  # Target dollar notional per order (min $1.00).
        max_order_contracts: int = None,  # Hard ceiling on contracts per single order.
        max_hedge_inventory: int = None,  # Hard ceiling on inventory hedge threshold.
        target_preference: Optional[str] = None,
        auto_rotate: bool = True,
        starvation_timeout: float = 900.0, # 15 minutes
        min_mid_price: Optional[int] = None,
        max_mid_price: Optional[int] = None,
    ):
        self.ticker = ticker
        self.gamma = gamma if gamma is not None else RISK_GAMMA
        self.min_spread = min_spread if min_spread is not None else MIN_SPREAD
        self.order_size = order_size if order_size is not None else ORDER_SIZE
        self.max_order_contracts = max(1, int(max_order_contracts)) if max_order_contracts is not None else MAX_ORDER_CONTRACTS
        self.max_hedge_inventory = max(5, int(max_hedge_inventory)) if max_hedge_inventory is not None else MAX_HEDGE_INVENTORY
        self.target_preference = target_preference if target_preference is not None else ticker
        self.auto_rotate = auto_rotate
        self.starvation_timeout = starvation_timeout
        self.min_mid_price = min_mid_price if min_mid_price is not None else MIN_MID_PRICE
        self.max_mid_price = max_mid_price if max_mid_price is not None else MAX_MID_PRICE


        # Determine order_dollars with strict $1.00 minimum enforcement and non-finite boundary safety
        if order_dollars is not None:
            try:
                od = float(order_dollars)
                self.order_dollars = max(1.0, od) if math.isfinite(od) else 1.0
            except (ValueError, TypeError):
                self.order_dollars = 1.0
        elif order_size is not None:
            # Caller explicitly passed fixed order_size (e.g. in deterministic unit tests)
            self.order_dollars = None
        else:
            self.order_dollars = max(1.0, float(ORDER_DOLLARS))

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
        self.current_bid_size: Optional[int] = None
        self.current_ask_size: Optional[int] = None
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

        # Background task references to prevent garbage collection in asyncio
        self._background_tasks = set()
        self._sync_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None

    def calculate_order_size(self, mid_price: Optional[float]) -> int:
        """
        Calculates the order size (contract count) for the current quoting cycle.
        If order_dollars is configured (>= $1.00), dynamically calculates the contract count
        required to deploy at least order_dollars at the current mid-price.
        Guaranteed to deploy at least $1.00 and never lower.
        Bounds midpoint to Kalshi's supported quoting limits [1.0, 99.0] and enforces
        a hard pre-submission ceiling (self.max_order_contracts) to prevent runaway
        or manipulated contract counts.
        Otherwise falls back to fixed order_size (also capped by max_order_contracts).
        """
        if self.order_dollars is not None and math.isfinite(self.order_dollars) and self.order_dollars >= 1.0:
            if isinstance(mid_price, (int, float)) and not isinstance(mid_price, bool) and math.isfinite(mid_price) and mid_price > 0:
                # Bound midpoint to Kalshi's valid exchange quoting limits [1c, 99c]
                # to prevent runaway contract counts on extreme/sub-cent books (e.g. 0.05c -> 2,000 contracts)
                effective_price = max(1.0, min(99.0, float(mid_price)))
                raw_count = max(1, math.ceil((self.order_dollars * 100.0) / effective_price))
                return min(self.max_order_contracts, raw_count)
        fallback = self.order_size if (isinstance(self.order_size, int) and not isinstance(self.order_size, bool) and self.order_size > 0) else 1
        return min(self.max_order_contracts, max(1, fallback))



    async def start(self):
        """Initializes infrastructure and starts the main trading loop."""
        logger.info(f"Starting Market Maker for {self.ticker}")
        
        # Start Prometheus Metrics Server
        start_metrics_server(port=8000)

        # 0. Recover state and cancel orphaned orders
        await self.om.sync_and_recover_state()
        
        # 1. Start the WebSocket Connection
        ws_task = asyncio.create_task(self.ws_client.connect())
        self._background_tasks.add(ws_task)
        ws_task.add_done_callback(self._background_tasks.discard)
        
        # 2. Wait for connection to establish
        while not self.ws_client.is_connected:
            await asyncio.sleep(0.1)
            
        logger.info("WebSocket Connected. Hydrating state...")
        
        # 3. Hydrate initial inventory and subscribe to channels
        if not await self.inv_manager.hydrate(is_startup=True):
            logger.critical("Failed to hydrate initial inventory state from REST API on startup. Aborting startup.")
            await send_alert(f"Startup Aborted: Failed to hydrate initial portfolio inventory for {self.ticker}.")
            self.running = False
            raise RuntimeError(f"Startup hydration failed for {self.ticker}; cannot safely trade without verified position ground truth.")
        
        # 3b. Launch the periodic reconciliation background task
        self._sync_task = asyncio.create_task(self.inv_manager._sync_loop())
        self._background_tasks.add(self._sync_task)
        self._sync_task.add_done_callback(self._background_tasks.discard)
        
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
            raise

    async def _tick(self):
        """The core logic evaluated every cycle."""
        now = time.time()

        # Track PnL and Inventory immediately regardless of orderbook state so metrics always show up on Grafana
        inventory = self.inv_manager.get_position(self.ticker)
        BOT_INVENTORY_NET_POSITION.labels(ticker=self.ticker).set(inventory)
        
        balance = self.inv_manager.get_balance()
        BOT_PNL_CENTS.labels(ticker=self.ticker).set(balance)

        try:
            KALSHI_REALIZED_PNL_CENTS.labels(ticker=self.ticker).set(self.inv_manager.pnl_tracker.get_realized_pnl(self.ticker))
            KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=self.ticker).set(self.inv_manager.pnl_tracker.get_unrealized_pnl(self.ticker))
            KALSHI_FEES_PAID_CENTS.labels(ticker=self.ticker).set(self.inv_manager.pnl_tracker.get_total_fees(self.ticker))
        except Exception as e_m:
            logger.debug(f"Telemetry update skipped for {self.ticker}: {e_m}")

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
            self._maybe_schedule_pnl_snapshot(now, inventory)
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
                    self._maybe_schedule_pnl_snapshot(now, inventory)
                    return
                else:
                    logger.error(f"No replacement market found for {self.ticker}.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
                    await self._cancel_all_quotes()
                    self._maybe_schedule_pnl_snapshot(now, inventory)
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
                            self._maybe_schedule_pnl_snapshot(now, inventory)
                            return
                        else:
                            logger.error(f"No replacement market found for starved market {self.ticker}.")
                            self._market_inactive = True
                            self._last_inactive_retry = now
                            await self._cancel_all_quotes()
                            self._maybe_schedule_pnl_snapshot(now, inventory)
                            return

                if now - self._last_empty_ob_log > 30:
                    logger.warning(
                        f"Orderbook for {self.ticker} has no two-sided quotes "
                        f"(best_bid={best_bid}, best_ask={best_ask}, starved={int(starved_duration)}s). "
                        f"Waiting for market activity..."
                    )
                    self._last_empty_ob_log = now
                await self._cancel_all_quotes()
                self._maybe_schedule_pnl_snapshot(now, inventory)
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
        self._maybe_schedule_pnl_snapshot(now, inventory)

        # 2a. Extreme Price Collar Safeguard:
        # If orderbook midpoint drifts outside [min_mid_price, max_mid_price],
        # immediately cancel active quotes to prevent late-game blowout adverse selection.
        if mid_price < self.min_mid_price or mid_price > self.max_mid_price:
            logger.warning(
                f"[PRICE COLLAR BREACH] Market {self.ticker} mid price {mid_price:.2f}c is outside "
                f"safety collar [{self.min_mid_price}c, {self.max_mid_price}c]. "
                f"Cancelling active quotes and initiating rotation..."
            )
            try:
                SAFEGUARD_EVENTS_TOTAL.labels(safeguard="price_collar", ticker=self.ticker).inc()
            except Exception:
                pass
            await self._cancel_all_quotes()
            if self.auto_rotate:
                replacement = await discover_active_market_async(
                    target_preference=self.target_preference,
                    exclude_tickers=[self.ticker]
                )
                if replacement:
                    if await self.rotate_market(replacement):
                        self._market_inactive = False
                        logger.info(f"Successfully rotated from collar-breached market to {replacement}.")
                    else:
                        self._market_inactive = True
                        self._last_inactive_retry = now
                        logger.warning(f"Rotation to {replacement} aborted; will retry in {self._inactive_retry_interval}s.")
                else:
                    logger.error(f"No replacement market found for collar-breached market {self.ticker}.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
            else:
                self._market_inactive = True
            return

        # 3. Get Inventory
        # Convention: positive inventory = holding net YES
        
        # Determine dynamic quote size for current market price
        quote_size = self.calculate_order_size(mid_price)
        self._current_quote_size = quote_size

        # 4. Calculate Reservation Price (R)
        # In multi-contract dynamic sizing, inventory is normalized by quote lot size:
        # q = inventory / quote_size
        normalized_q = (inventory / quote_size) if (self.order_dollars and quote_size > 0) else inventory
        reservation_price = mid_price - (normalized_q * self.gamma)
        
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
        # Threshold scales with quote size (5 order units), bounded by max_hedge_inventory
        base_hedge_threshold = (5 * quote_size) if self.order_dollars else 5
        hedge_threshold = min(self.max_hedge_inventory, base_hedge_threshold)
        if inventory >= hedge_threshold:
            logger.warning(f"[HEDGE ACTIVE] Long inventory high ({inventory} >= {hedge_threshold}). Halting BIDs, crossing ASKs to exit.")
            optimal_bid = None # Do not buy more YES
            if best_bid:
                optimal_ask = max(2, min(best_bid[0], 99)) # Match the best bid to fill immediately
        elif inventory <= -hedge_threshold:
            logger.warning(f"[HEDGE ACTIVE] Short inventory high ({inventory} <= -{hedge_threshold}). Halting ASKs, crossing BIDs to exit.")
            optimal_ask = None # Do not sell more YES
            if best_ask:
                optimal_bid = max(1, min(best_ask[0], 98)) # Match the best ask to fill immediately

        realized_pnl = self.inv_manager.get_realized_pnl(self.ticker)
        unrealized_pnl = self.inv_manager.get_unrealized_pnl(self.ticker)
        if optimal_bid is not None and optimal_ask is not None:
            logger.info(
                f"[A-S MATH] Mid={mid_price:.1f}c | Size={quote_size} | Inventory={inventory} | Gamma={self.gamma} | "
                f"ReservationPrice={reservation_price:.2f}c | Spread={self.min_spread}c "
                f"→ Bid={optimal_bid}c  Ask={optimal_ask}c | "
                f"Realized={realized_pnl:+.1f}c | Unrealized={unrealized_pnl:+.1f}c"
            )
        else:
            logger.info(
                f"[A-S MATH] Mid={mid_price:.1f}c | Size={quote_size} | Inventory={inventory} | "
                f"→ Bid={optimal_bid}c  Ask={optimal_ask}c (Hedged) | "
                f"Realized={realized_pnl:+.1f}c | Unrealized={unrealized_pnl:+.1f}c"
            )
            
        # 5. Execute Output
        await self._update_quotes(optimal_bid, optimal_ask)

    async def _update_quotes(self, new_bid: Optional[Union[int, float]], new_ask: Optional[Union[int, float]]):
        """Places or replaces quotes if the optimal prices or contract sizes have shifted."""
        cancel_tasks = []
        cancel_bid = False
        cancel_ask = False

        # Determine target quote sizes for bid and ask
        target_bid_size = getattr(self, "_current_quote_size", None)
        if not isinstance(target_bid_size, int) or isinstance(target_bid_size, bool) or target_bid_size <= 0:
            target_bid_size = self.order_size if (isinstance(self.order_size, int) and not isinstance(self.order_size, bool) and self.order_size > 0) else 1

        target_ask_size = getattr(self, "_current_quote_size", None)
        if not isinstance(target_ask_size, int) or isinstance(target_ask_size, bool) or target_ask_size <= 0:
            target_ask_size = self.order_size if (isinstance(self.order_size, int) and not isinstance(self.order_size, bool) and self.order_size > 0) else 1

        # Check whether quotes need update (price change OR contract size change on active quote)
        bid_needs_update = (new_bid != self.current_bid_price) or (new_bid is not None and target_bid_size != self.current_bid_size)
        ask_needs_update = (new_ask != self.current_ask_price) or (new_ask is not None and target_ask_size != self.current_ask_size)

        # Handle BID Side
        if bid_needs_update:
            if self.current_bid_id:
                cancel_tasks.append(self.om.cancel_order(self.current_bid_id))
                cancel_bid = True

        # Handle ASK Side (Selling YES contracts)
        if ask_needs_update:
            if self.current_ask_id:
                cancel_tasks.append(self.om.cancel_order(self.current_ask_id))
                cancel_ask = True

        # Execute cancellations before clearing IDs so that IDs are only cleared on success
        if cancel_tasks:
            results = await asyncio.gather(*cancel_tasks, return_exceptions=True)
            result_iter = iter(results)
            if cancel_bid:
                result = next(result_iter)
                if result is True:
                    self.current_bid_id = None
                    self.current_bid_price = None
                    self.current_bid_size = None
                else:
                    logger.error(
                        f"Failed to confirm cancellation of bid quote {self.current_bid_id} "
                        f"(result: {result!r}); retaining active quote state to prevent duplicate quoting."
                    )
            if cancel_ask:
                result = next(result_iter)
                if result is True:
                    self.current_ask_id = None
                    self.current_ask_price = None
                    self.current_ask_size = None
                else:
                    logger.error(
                        f"Failed to confirm cancellation of ask quote {self.current_ask_id} "
                        f"(result: {result!r}); retaining active quote state to prevent duplicate quoting."
                    )


        # Place new BID if needed
        if bid_needs_update:
            if new_bid is not None and self.current_bid_id is None:
                logger.info(f">> Placing new BID: {target_bid_size} YES @ {new_bid}c")
                self.current_bid_id = await self.om.place_order(
                    ticker=self.ticker, side="yes", action="buy", count=target_bid_size, price=new_bid
                )
                if self.current_bid_id:
                    self.current_bid_price = new_bid
                    self.current_bid_size = target_bid_size
                else:
                    self.current_bid_price = None
                    self.current_bid_size = None

        # Place new ASK if needed
        if ask_needs_update:
            if new_ask is not None and self.current_ask_id is None:
                logger.info(f">> Placing new ASK: {target_ask_size} YES @ {new_ask}c")
                self.current_ask_id = await self.om.place_order(
                    ticker=self.ticker, side="yes", action="sell", count=target_ask_size, price=new_ask
                )
                if self.current_ask_id:
                    self.current_ask_price = new_ask
                    self.current_ask_size = target_ask_size
                else:
                    self.current_ask_price = None
                    self.current_ask_size = None



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
                self.current_bid_size = None
            else:
                self.current_ask_id = None
                self.current_ask_price = None
                self.current_ask_size = None

        return all_cancelled

    def _maybe_schedule_pnl_snapshot(self, now: float, inventory: Optional[int] = None) -> None:
        """Schedule periodic PnL snapshot persistence to PostgreSQL if the interval has elapsed."""
        if now - self._last_pnl_snapshot >= self._pnl_snapshot_interval:
            self._last_pnl_snapshot = now
            current_ticker = self.ticker
            current_inventory = self.inv_manager.get_position(current_ticker)
            pnl_summary = self.inv_manager.get_pnl_summary(current_ticker)

            # Serialize snapshot writes: skip if previous background snapshot is still writing
            if self._snapshot_task and not self._snapshot_task.done():
                logger.debug("Previous PnL snapshot task is still in flight; skipping periodic snapshot.")
                return

            snap_task = asyncio.create_task(
                self.om.record_pnl_snapshot_async(
                    ticker=current_ticker,
                    realized_pnl_cents=pnl_summary.get("realized_pnl_cents", 0.0),
                    unrealized_pnl_cents=pnl_summary.get("unrealized_pnl_cents", 0.0),
                    total_fees_cents=pnl_summary.get("total_fees_cents", 0.0),
                    inventory=current_inventory,
                    rotation_session_id=pnl_summary.get("rotation_session_id", "")
                )
            )
            self._snapshot_task = snap_task
            self._background_tasks.add(snap_task)
            snap_task.add_done_callback(self._background_tasks.discard)

    async def _escalate_to_kill_switch(self, context: str = "operation") -> bool:
        """
        Escalates to emergency KillSwitch, canceling all orders in om.active_orders
        and explicitly canceling and verifying untracked current_bid_id/current_ask_id.
        Returns True if all active orders and quotes are confirmed cleared, False otherwise.
        """
        logger.error(f"Active orders remain or quote cancellation failed during {context}. Escalating to emergency kill switch...")
        try:
            from execution.kill_switch import KillSwitch
            killer = KillSwitch(self.om)
            initial_active = set(self.om.active_orders.keys())
            await killer.trigger()

            # For quotes that were in om.active_orders, verify killer successfully popped them
            # For untracked quotes, explicitly cancel on exchange via om.cancel_order
            if self.current_bid_id:
                if self.current_bid_id in initial_active:
                    if self.current_bid_id not in self.om.active_orders:
                        self.current_bid_id = None
                        self.current_bid_price = None
                        self.current_bid_size = None
                    else:
                        logger.error(f"KillSwitch failed to cancel tracked bid quote {self.current_bid_id}")
                else:
                    if await self.om.cancel_order(self.current_bid_id):
                        self.current_bid_id = None
                        self.current_bid_price = None
                        self.current_bid_size = None
                    else:
                        logger.error(f"Failed to confirm cancellation of untracked resting bid quote {self.current_bid_id}")

            if self.current_ask_id:
                if self.current_ask_id in initial_active:
                    if self.current_ask_id not in self.om.active_orders:
                        self.current_ask_id = None
                        self.current_ask_price = None
                        self.current_ask_size = None
                    else:
                        logger.error(f"KillSwitch failed to cancel tracked ask quote {self.current_ask_id}")
                else:
                    if await self.om.cancel_order(self.current_ask_id):
                        self.current_ask_id = None
                        self.current_ask_price = None
                        self.current_ask_size = None
                    else:
                        logger.error(f"Failed to confirm cancellation of untracked resting ask quote {self.current_ask_id}")


            return not bool(self.current_bid_id or self.current_ask_id or self.om.active_orders)
        except Exception as e:
            logger.critical(f"Emergency kill switch failed during {context}: {e}")
            return False

    async def rotate_market(self, new_ticker: str) -> bool:
        """
        Dynamically rotate bot quoting and orderbook subscription to a new market ticker
        without restarting the container process.
        """
        old_ticker = self.ticker
        if new_ticker == old_ticker:
            return True

        logger.warning(f"Rotating target market from {old_ticker} to {new_ticker}...")

        # 1. Withdraw all active quotes on the previous ticker and ensure no active orders remain
        quotes_cancelled = await self._cancel_all_quotes()
        has_active_orders = bool(self.current_bid_id or self.current_ask_id or self.om.active_orders)
        if not quotes_cancelled or has_active_orders:
            logger.error(
                f"Aborting market rotation from {old_ticker} to {new_ticker}; "
                f"active orders remain or quote cancellation was not confirmed."
            )
            await self._escalate_to_kill_switch(context=f"market rotation from {old_ticker} to {new_ticker}")
            return False

        # Yield to event loop to quiesce in-flight fill processing and drain running snapshot tasks
        await asyncio.sleep(0)
        if self._snapshot_task and not self._snapshot_task.done():
            try:
                await self._snapshot_task
            except Exception as e:
                logger.warning(f"Error awaiting previous snapshot task during rotation: {e}")

        # Persist final PnL attribution snapshot for the market being rotated out (after resting quotes are confirmed cancelled)
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
        self._last_pnl_snapshot = time.time()

        # Start a new tracking session for the new market ticker
        self.inv_manager.pnl_tracker.reset_market_session(new_ticker)

        logger.info(f"Market rotation complete. Now trading {new_ticker}.")
        await send_alert(f"Market Rotated: Switched target from {old_ticker} to active market {new_ticker}.")
        return True

    async def stop(self) -> bool:
        self.running = False

        # 0. Cancel background reconciliation loop before capturing shutdown state
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except (asyncio.CancelledError, Exception):
                pass

        # 1. Withdraw resting quotes first to eliminate market risk immediately
        quotes_cancelled = await self._cancel_all_quotes()
        has_active_orders = bool(self.current_bid_id or self.current_ask_id or self.om.active_orders)
        if not quotes_cancelled or has_active_orders:
            quotes_cancelled = await self._escalate_to_kill_switch(context="shutdown")

        # Quiesce fills and drain running background snapshot tasks before final write
        await asyncio.sleep(0)
        if self._snapshot_task and not self._snapshot_task.done():
            try:
                await self._snapshot_task
            except Exception as e:
                logger.warning(f"Error awaiting previous snapshot task during shutdown: {e}")

        # 2. Persist final shutdown snapshot after quotes are withdrawn
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

        # 3. Cancel and await all active background tasks
        tasks_to_cancel = [t for t in list(self._background_tasks) if not t.done()]
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        return quotes_cancelled
