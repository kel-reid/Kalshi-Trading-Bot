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
import datetime
from collections import deque
from decimal import Decimal, InvalidOperation
from typing import Optional, Union

from data.websocket_client import KalshiWebsocketClient
from data.orderbook_manager import OrderbookManager
from data.inventory_manager import InventoryManager
from execution.order_manager import OrderManager
from utils.alerting import send_alert
from utils.market_discovery import (
    check_market_status_async,
    discover_active_market_async,
    get_market_details_async,
    get_raw_market_status_async,
    is_market_active_async,
    get_market_exchange_index,
)
from utils.horizon import _get_earliest_market_timestamp
from utils.sports_router import SportsSeasonRouter
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
    MAX_SESSION_FEES_CENTS,
    MAX_SESSION_LOSS_CENTS,
    POST_FILL_PAUSE_SECONDS,
    PRICE_VELOCITY_THRESHOLD_CENTS,
    PRICE_VELOCITY_WINDOW_SECONDS,
    PRICE_VELOCITY_QUIESCE_SECONDS,
    SLATE_REEVALUATION_INTERVAL_SECONDS,
    MIN_MARKET_DWELL_SECONDS,
    DYNAMIC_SPREAD_ENABLED,
    TARGET_EDGE_CENTS,
    MIN_DYNAMIC_SPREAD_CENTS,
    MAX_DYNAMIC_SPREAD_CENTS,
    VOLATILITY_WINDOW_SECONDS,
    KALSHI_FEE_RATE,
)
from strategy.dynamic_spread import (
    RollingVolatilityEstimator,
    calculate_optimal_spread,
    calculate_fee_cents,
    calculate_spread_floor_cents,
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
        max_session_fees_cents: Optional[int] = None,
        max_session_loss_cents: Optional[int] = None,
        post_fill_pause_seconds: Optional[float] = None,
        price_velocity_threshold_cents: Optional[float] = None,
        price_velocity_window_seconds: Optional[float] = None,
        price_velocity_quiesce_seconds: Optional[float] = None,
        dynamic_spread_enabled: Optional[bool] = None,
        target_edge_cents: Optional[float] = None,
        min_dynamic_spread_cents: Optional[int] = None,
        max_dynamic_spread_cents: Optional[int] = None,
        volatility_window_seconds: Optional[float] = None,
        kalshi_fee_rate: Optional[float] = None,
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
        self.max_session_fees_cents = max_session_fees_cents if max_session_fees_cents is not None else MAX_SESSION_FEES_CENTS
        self.max_session_loss_cents = max_session_loss_cents if max_session_loss_cents is not None else MAX_SESSION_LOSS_CENTS
        self.post_fill_pause_seconds = (
            max(0.0, float(post_fill_pause_seconds))
            if post_fill_pause_seconds is not None
            else POST_FILL_PAUSE_SECONDS
        )
        self.price_velocity_threshold_cents = (
            max(0.0, float(price_velocity_threshold_cents))
            if price_velocity_threshold_cents is not None
            else PRICE_VELOCITY_THRESHOLD_CENTS
        )
        self.price_velocity_window_seconds = (
            max(0.0, float(price_velocity_window_seconds))
            if price_velocity_window_seconds is not None
            else PRICE_VELOCITY_WINDOW_SECONDS
        )
        self.price_velocity_quiesce_seconds = (
            max(0.0, float(price_velocity_quiesce_seconds))
            if price_velocity_quiesce_seconds is not None
            else PRICE_VELOCITY_QUIESCE_SECONDS
        )

        # Dynamic Volatility & Fee-Aware Spread Configuration
        if dynamic_spread_enabled is not None:
            self.dynamic_spread_enabled = bool(dynamic_spread_enabled)
        elif min_spread is not None:
            # Caller explicitly passed static min_spread (e.g. in deterministic unit tests)
            self.dynamic_spread_enabled = False
        else:
            self.dynamic_spread_enabled = DYNAMIC_SPREAD_ENABLED

        self.target_edge_cents = (
            float(target_edge_cents)
            if target_edge_cents is not None
            else TARGET_EDGE_CENTS
        )
        self.min_dynamic_spread_cents = (
            int(min_dynamic_spread_cents)
            if min_dynamic_spread_cents is not None
            else MIN_DYNAMIC_SPREAD_CENTS
        )
        self.max_dynamic_spread_cents = (
            int(max_dynamic_spread_cents)
            if max_dynamic_spread_cents is not None
            else MAX_DYNAMIC_SPREAD_CENTS
        )
        self.volatility_window_seconds = (
            max(5.0, float(volatility_window_seconds))
            if volatility_window_seconds is not None
            else VOLATILITY_WINDOW_SECONDS
        )
        self.kalshi_fee_rate = (
            float(kalshi_fee_rate)
            if kalshi_fee_rate is not None
            else KALSHI_FEE_RATE
        )
        self.volatility_estimator = RollingVolatilityEstimator(
            window_seconds=self.volatility_window_seconds
        )


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

        # Periodic market settlement/status check (initialized to 0.0 to enforce on startup tick)
        self._last_market_status_check: float = 0.0
        self._market_status_check_interval: float = 60.0
        self._market_inactive: bool = False
        self._last_inactive_retry: float = 0.0
        self._inactive_retry_interval: float = 5.0

        # Periodic PnL snapshot tracking
        self._last_pnl_snapshot: float = time.time()
        self._pnl_snapshot_interval: float = 60.0

        # Order placement failure cooldown tracking to prevent runaway rate limit spam
        self._last_order_error_time: float = 0.0
        self._order_error_cooldown: float = 5.0

        # Periodic exchange resting order reconciliation
        self._last_reconciliation_time: float = time.time()
        self._reconciliation_interval: float = 60.0

        # Fast Market / Price Velocity Circuit Breaker tracking
        self._mid_price_history: deque = deque()
        self._fast_market_until: float = 0.0

        # Periodic active slate and event-horizon re-evaluation
        # Re-evaluates whether current market is off-slate/distant while inventory is flat
        self._last_slate_check: float = time.time()
        self._slate_check_interval: float = float(SLATE_REEVALUATION_INTERVAL_SECONDS)
        self._min_market_dwell_time: float = float(MIN_MARKET_DWELL_SECONDS)
        self._market_attached_time: float = time.time()

        # Background task references to prevent garbage collection in asyncio
        self._background_tasks = set()
        self._sync_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None

        # Exchange Shard Routing & Hedging Circuit Breaker
        self.exchange_index: Optional[int] = get_market_exchange_index(self.ticker)
        self._consecutive_hedge_failures: int = 0
        self._last_shard_rebalance_time: float = 0.0

    async def ensure_shard_collateral(self, min_required_dollars: float = 15.0) -> bool:
        """
        Ensures that the current market's exchange shard has sufficient collateral (min_required_dollars).
        If the market resides on a non-default shard (e.g. Shard 3 for sports/NBA) and collateral is low,
        initiates an intra-exchange instance transfer from Shard 0 (main balance).
        """
        if getattr(self, "exchange_index", None) is None:
            self.exchange_index = await asyncio.to_thread(get_market_exchange_index, self.ticker)

        target_shard = getattr(self, "exchange_index", None)
        if target_shard is None:
            logger.warning(f"Could not resolve exchange_index for {self.ticker}; cannot verify shard collateral.")
            return False

        if target_shard == 0:
            return True

        now = time.time()
        # Enforce a 10s cooldown between rebalances to prevent transfer spam
        if now - self._last_shard_rebalance_time < 10.0:
            logger.debug(f"Shard collateral check for Shard {target_shard} in cooldown.")
            return True

        try:
            balances = await asyncio.to_thread(self.om.get_portfolio_balances)
            shard_balances = balances.get("shard_balances", {})
            current_shard_balance = float(shard_balances.get(target_shard, 0.0))
            shard0_balance = float(shard_balances.get(0, 0.0))

            if current_shard_balance >= min_required_dollars:
                return True

            needed_dollars = round(min_required_dollars - current_shard_balance, 2)
            if needed_dollars <= 0.0:
                return True

            logger.warning(
                f"Shard {target_shard} balance (${current_shard_balance:.2f}) below required "
                f"${min_required_dollars:.2f}. Initiating rebalance from Shard 0 (balance: ${shard0_balance:.2f})."
            )

            # Check if Shard 0 has funds to cover
            if shard0_balance < needed_dollars:
                transfer_amount = max(0.0, round(shard0_balance - 1.0, 2))
                if transfer_amount < 1.0:
                    logger.error(
                        f"Cannot transfer to Shard {target_shard}: Shard 0 balance (${shard0_balance:.2f}) "
                        f"insufficient for minimum transfer ($1.00)."
                    )
                    return False
            else:
                transfer_amount = needed_dollars

            transfer_ok = await asyncio.to_thread(
                self.om.transfer_intra_exchange,
                source_shard=0,
                destination_shard=target_shard,
                amount_dollars=transfer_amount,
            )
            if transfer_ok:
                self._last_shard_rebalance_time = time.time()
                logger.info(
                    f"Accepted transfer of ${transfer_amount:.2f} to Shard {target_shard} from Shard 0; "
                    f"awaiting settlement..."
                )
                for attempt in range(6):
                    await asyncio.sleep(2.0)
                    balances = await asyncio.to_thread(self.om.get_portfolio_balances)
                    shard_balances = balances.get("shard_balances", {})
                    current_shard_balance = float(shard_balances.get(target_shard, 0.0))
                    if current_shard_balance >= min_required_dollars:
                        logger.info(
                            f"Confirmed Shard {target_shard} funded: balance=${current_shard_balance:.2f} "
                            f"(attempt {attempt + 1}/6)."
                        )
                        return True
                logger.error(
                    f"Shard {target_shard} balance (${current_shard_balance:.2f}) not funded with required "
                    f"${min_required_dollars:.2f} after transfer acceptance."
                )
                return False
            else:
                logger.error(f"Failed to fund Shard {target_shard} from Shard 0.")
                return False
        except Exception as e:
            logger.error(f"Exception ensuring shard collateral for Shard {target_shard}: {e}")
            return False

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
        
        # 3a. Ensure exchange shard collateral before quoting starts
        if getattr(self, "exchange_index", None) is None:
            self.exchange_index = await asyncio.to_thread(get_market_exchange_index, self.ticker)
        await self.ensure_shard_collateral()

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
            KALSHI_FEES_PAID_CENTS.labels(ticker=self.ticker).set(self.inv_manager.pnl_tracker.get_total_fees(self.ticker))
        except Exception as e_m:
            logger.debug(f"Telemetry update skipped for {self.ticker}: {e_m}")

        # 0. Active Inactive Market Recovery: Retry unconfirmed cancellations, retry liquidation, and retry finding replacement
        if self._market_inactive:
            if self.current_bid_id or self.current_ask_id:
                logger.warning(f"Retrying quote cancellation for inactive market {self.ticker}...")
                await self._cancel_all_quotes()

            if now - self._last_inactive_retry >= self._inactive_retry_interval:
                self._last_inactive_retry = now
                if self.inv_manager.get_position(self.ticker) != 0:
                    logger.warning(f"Retrying liquidation for inactive market {self.ticker}...")
                    if not await self.liquidate_inventory(self.ticker):
                        hydrated = False
                        try:
                            hydrated = await self.inv_manager.hydrate(is_startup=False)
                        except Exception as e_hyd:
                            logger.error(f"Failed to hydrate inventory on inactive retry for {self.ticker}: {e_hyd}")
                        if not hydrated:
                            await send_alert(
                                f"⚠️ Inactive market liquidation could not be confirmed for {self.ticker}; "
                                "position state is unknown after inventory hydration failed."
                            )
                        elif self.inv_manager.get_position(self.ticker) != 0:
                            await send_alert(
                                f"⚠️ Inactive market liquidation retry failed for {self.ticker} "
                                f"(position={self.inv_manager.get_position(self.ticker)})."
                            )

                if self.auto_rotate:
                    logger.info(f"Retrying market discovery for inactive market {self.ticker}...")
                    replacement = await discover_active_market_async(
                        target_preference=self.target_preference,
                        exclude_tickers=[self.ticker],
                        min_mid_price=self.min_mid_price,
                        max_mid_price=self.max_mid_price,
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

        # Periodic market settlement and expiry detection (evaluated on startup tick and every interval)
        if now - self._last_market_status_check >= self._market_status_check_interval:
            self._last_market_status_check = now
            is_active = await is_market_active_async(self.ticker)
            if is_active is False:
                logger.warning(f"Market {self.ticker} is no longer active (settled, expired, or reached cutoff). Withdrawing quotes...")
                try:
                    SAFEGUARD_EVENTS_TOTAL.labels(safeguard="expiry_cutoff", ticker=self.ticker).inc()
                except Exception as e_metric:
                    logger.debug(f"Safeguard metric update failed: {e_metric}")
                await self._cancel_all_quotes()
                if self.auto_rotate:
                    replacement = await discover_active_market_async(
                        target_preference=self.target_preference,
                        exclude_tickers=[self.ticker],
                        min_mid_price=self.min_mid_price,
                        max_mid_price=self.max_mid_price,
                    )
                    if replacement:
                        if await self.rotate_market(replacement):
                            self._market_inactive = False
                            logger.info(f"Successfully rotated from inactive market to {replacement}.")
                        else:
                            self._market_inactive = True
                            self._last_inactive_retry = now
                            logger.warning(f"Rotation to {replacement} aborted; will retry in {self._inactive_retry_interval}s.")
                    else:
                        logger.error(f"No replacement market found for {self.ticker}.")
                        if inventory != 0:
                            if not await self.liquidate_inventory(self.ticker):
                                hydrated = False
                                try:
                                    hydrated = await self.inv_manager.hydrate(is_startup=False)
                                except Exception as e_hyd:
                                    logger.error(f"Failed to hydrate inventory during cutoff for {self.ticker}: {e_hyd}")
                                if not hydrated or self.inv_manager.get_position(self.ticker) != 0:
                                    await send_alert(f"⚠️ Safeguard liquidation for {self.ticker} is unconfirmed.")
                        self._market_inactive = True
                        self._last_inactive_retry = now
                else:
                    if inventory != 0:
                        if not await self.liquidate_inventory(self.ticker):
                            hydrated = False
                            try:
                                hydrated = await self.inv_manager.hydrate(is_startup=False)
                            except Exception as e_hyd:
                                logger.error(f"Failed to hydrate inventory during cutoff for {self.ticker}: {e_hyd}")
                            if not hydrated or self.inv_manager.get_position(self.ticker) != 0:
                                await send_alert(f"⚠️ Safeguard liquidation for {self.ticker} is unconfirmed.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
                self._maybe_schedule_pnl_snapshot(now, inventory)
                return
            elif is_active is True:
                self._market_inactive = False
            else:
                logger.warning(f"Unable to confirm market status for {self.ticker}; retaining current market.")

        # Periodic active slate and event-horizon re-evaluation (only when inventory is 0 and dwell time elapsed)
        if now - self._last_slate_check >= self._slate_check_interval:
            self._last_slate_check = now
            if (
                self.auto_rotate
                and not self._market_inactive
                and inventory == 0
                and (now - self._market_attached_time >= self._min_market_dwell_time)
            ):
                if await self._maybe_recheck_slate(now):
                    self._maybe_schedule_pnl_snapshot(now, inventory)
                    return
                # Refresh inventory from inv_manager following async discovery check
                inventory = self.inv_manager.get_position(self.ticker)

        
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
                            exclude_tickers=[self.ticker],
                            min_mid_price=self.min_mid_price,
                            max_mid_price=self.max_mid_price,
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
                            if inventory != 0:
                                if not await self.liquidate_inventory(self.ticker):
                                    await send_alert(f"⚠️ Safeguard liquidation failed for starved market {self.ticker}; open position remains unhedged.")
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
        
        # 2. Mark open inventory to market against current mid price and update unrealized PnL gauge
        self.inv_manager.update_orderbook_mid(self.ticker, mid_price)
        try:
            KALSHI_UNREALIZED_PNL_CENTS.labels(ticker=self.ticker).set(
                self.inv_manager.pnl_tracker.get_unrealized_pnl(self.ticker)
            )
        except Exception as e_m:
            logger.debug(f"Telemetry unrealized update skipped for {self.ticker}: {e_m}")
        self._maybe_schedule_pnl_snapshot(now, inventory)

        # Periodic exchange resting order reconciliation
        if now - self._last_reconciliation_time >= self._reconciliation_interval:
            self._last_reconciliation_time = now
            rec_task = asyncio.create_task(self.om.reconcile_resting_orders(ticker=self.ticker))
            self._background_tasks.add(rec_task)
            rec_task.add_done_callback(self._background_tasks.discard)

        # 3. Determine dynamic quote size and hedge threshold for current market price
        quote_size = self.calculate_order_size(mid_price)
        self._current_quote_size = quote_size
        base_hedge_threshold = (5 * quote_size) if self.order_dollars else 5
        hedge_threshold = min(self.max_hedge_inventory, base_hedge_threshold)

        # 2b. Session Risk Safeguards (Fee Churn Circuit Breaker & Max Loss Stop-Loss)
        pnl_summary = self.inv_manager.get_pnl_summary(self.ticker)
        session_fees = pnl_summary.get("session_fees_cents", pnl_summary.get("total_fees_cents", 0.0))
        session_net_pnl = pnl_summary.get("session_realized_pnl_cents", pnl_summary.get("realized_pnl_cents", 0.0)) + pnl_summary.get("unrealized_pnl_cents", 0.0)

        if session_fees >= self.max_session_fees_cents or session_net_pnl <= -self.max_session_loss_cents:
            reason = "fee_churn" if session_fees >= self.max_session_fees_cents else "session_stop_loss"
            logger.warning(
                f"[{reason.upper()} TRIGGERED] Market {self.ticker} session metrics: "
                f"fees={session_fees:.1f}c (limit: {self.max_session_fees_cents}c), "
                f"net_pnl={session_net_pnl:.1f}c (loss limit: -{self.max_session_loss_cents}c)."
            )
            try:
                SAFEGUARD_EVENTS_TOTAL.labels(safeguard=reason, ticker=self.ticker).inc()
            except Exception as e_metric:
                logger.debug(f"Safeguard metric update failed: {e_metric}")

            # If inventory is at or above hedge threshold, allow ONLY the single-sided exit hedge to unwind exposure.
            if abs(inventory) >= hedge_threshold:
                if inventory >= hedge_threshold:
                    logger.warning(
                        f"[{reason.upper()} - HEDGE ACTIVE] Long inventory high ({inventory} >= {hedge_threshold}). "
                        f"Halting BIDs, crossing ASKs to exit."
                    )
                    optimal_bid = None
                    optimal_ask = max(2, min(best_bid[0], 99)) if best_bid else None
                else:
                    logger.warning(
                        f"[{reason.upper()} - HEDGE ACTIVE] Short inventory high ({inventory} <= -{hedge_threshold}). "
                        f"Halting ASKs, crossing BIDs to exit."
                    )
                    optimal_ask = None
                    optimal_bid = max(1, min(best_ask[0], 98)) if best_ask else None

                realized_pnl = self.inv_manager.get_realized_pnl(self.ticker)
                unrealized_pnl = self.inv_manager.get_unrealized_pnl(self.ticker)
                logger.info(
                    f"[A-S MATH] Mid={mid_price:.1f}c | Size={quote_size} | Inventory={inventory} | "
                    f"→ Bid={optimal_bid}c  Ask={optimal_ask}c ({reason} Hedged) | "
                    f"Realized={realized_pnl:+.1f}c | Unrealized={unrealized_pnl:+.1f}c"
                )
                await self._update_quotes(optimal_bid, optimal_ask)
                return

            # Otherwise (inventory below hedge threshold), cancel all active quotes and rotate/quiesce.
            await self._cancel_all_quotes()
            if self.auto_rotate:
                replacement = await discover_active_market_async(
                    target_preference=self.target_preference,
                    exclude_tickers=[self.ticker],
                    min_mid_price=self.min_mid_price,
                    max_mid_price=self.max_mid_price,
                )
                if replacement:
                    if await self.rotate_market(replacement):
                        self._market_inactive = False
                        logger.info(f"Successfully rotated from {reason} market to {replacement}.")
                    else:
                        self._market_inactive = True
                        self._last_inactive_retry = now
                else:
                    logger.error(f"No replacement market found after {reason} on {self.ticker}.")
                    if inventory != 0:
                        if not await self.liquidate_inventory(self.ticker):
                            await send_alert(f"⚠️ Safeguard liquidation failed after {reason} on {self.ticker}; open position remains unhedged.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
            else:
                if inventory != 0:
                    if not await self.liquidate_inventory(self.ticker):
                        await send_alert(f"⚠️ Safeguard liquidation failed after {reason} on {self.ticker}; open position remains unhedged.")
                self._market_inactive = True
                self._last_inactive_retry = now
            return

        # 2a. Extreme Price Collar Safeguard:
        # If orderbook midpoint drifts outside [min_mid_price, max_mid_price]:
        if mid_price < self.min_mid_price or mid_price > self.max_mid_price:
            logger.warning(
                f"[PRICE COLLAR BREACH] Market {self.ticker} mid price {mid_price:.2f}c is outside "
                f"safety collar [{self.min_mid_price}c, {self.max_mid_price}c]."
            )
            try:
                SAFEGUARD_EVENTS_TOTAL.labels(safeguard="price_collar", ticker=self.ticker).inc()
            except Exception as e_metric:
                logger.debug(f"Safeguard metric update failed: {e_metric}")

            # If inventory is at or above hedge threshold, allow ONLY the single-sided exit hedge to unwind exposure.
            if abs(inventory) >= hedge_threshold:
                if inventory >= hedge_threshold:
                    logger.warning(
                        f"[COLLAR BREACH - HEDGE ACTIVE] Long inventory high ({inventory} >= {hedge_threshold}). "
                        f"Halting BIDs outside collar, crossing ASKs to exit."
                    )
                    optimal_bid = None
                    optimal_ask = max(2, min(best_bid[0], 99)) if best_bid else None
                else:
                    logger.warning(
                        f"[COLLAR BREACH - HEDGE ACTIVE] Short inventory high ({inventory} <= -{hedge_threshold}). "
                        f"Halting ASKs outside collar, crossing BIDs to exit."
                    )
                    optimal_ask = None
                    optimal_bid = max(1, min(best_ask[0], 98)) if best_ask else None

                realized_pnl = self.inv_manager.get_realized_pnl(self.ticker)
                unrealized_pnl = self.inv_manager.get_unrealized_pnl(self.ticker)
                logger.info(
                    f"[A-S MATH] Mid={mid_price:.1f}c | Size={quote_size} | Inventory={inventory} | "
                    f"→ Bid={optimal_bid}c  Ask={optimal_ask}c (Collar Breach Hedged) | "
                    f"Realized={realized_pnl:+.1f}c | Unrealized={unrealized_pnl:+.1f}c"
                )
                await self._update_quotes(optimal_bid, optimal_ask)
                return

            # Otherwise (inventory below hedge threshold), cancel all active quotes and rotate/quiesce.
            await self._cancel_all_quotes()
            if self.auto_rotate:
                replacement = await discover_active_market_async(
                    target_preference=self.target_preference,
                    exclude_tickers=[self.ticker],
                    min_mid_price=self.min_mid_price,
                    max_mid_price=self.max_mid_price,
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
                    if inventory != 0:
                        if not await self.liquidate_inventory(self.ticker):
                            await send_alert(f"⚠️ Safeguard liquidation failed for collar-breached market {self.ticker}; open position remains unhedged.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
            else:
                if inventory != 0:
                    if not await self.liquidate_inventory(self.ticker):
                        await send_alert(f"⚠️ Safeguard liquidation failed for collar-breached market {self.ticker}; open position remains unhedged.")
                self._market_inactive = True
                self._last_inactive_retry = now
            return

        # 2c. Fast Market / Price Velocity Circuit Breaker:
        # Check active Fast Market quiesce period
        if self._fast_market_until > now:
            remaining = self._fast_market_until - now
            logger.info(
                f"[FAST MARKET ACTIVE] Volatility circuit breaker active for {self.ticker} "
                f"({remaining:.1f}s remaining). Withdrawing quotes."
            )
            if self.current_bid_id or self.current_ask_id:
                await self._cancel_all_quotes()
            return

        # Evaluate orderbook midpoint velocity over rolling window
        if self.price_velocity_threshold_cents > 0 and self.price_velocity_window_seconds > 0:
            cutoff = now - self.price_velocity_window_seconds
            while self._mid_price_history and self._mid_price_history[0][0] < cutoff:
                self._mid_price_history.popleft()

            self._mid_price_history.append((now, mid_price))

            if len(self._mid_price_history) >= 2:
                prices = [p for _, p in self._mid_price_history]
                delta_p = max(prices) - min(prices)
                if delta_p >= self.price_velocity_threshold_cents:
                    elapsed = now - self._mid_price_history[0][0]
                    logger.warning(
                        f"[FAST MARKET / VELOCITY BREACH] Market {self.ticker} mid price swung "
                        f"{delta_p:.1f}c over {elapsed:.1f}s (threshold: {self.price_velocity_threshold_cents:.1f}c). "
                        f"Entering Fast Market state; withdrawing quotes for {self.price_velocity_quiesce_seconds:.1f}s."
                    )
                    try:
                        SAFEGUARD_EVENTS_TOTAL.labels(safeguard="price_velocity", ticker=self.ticker).inc()
                    except Exception as e_metric:
                        logger.debug(f"Safeguard metric update failed: {e_metric}")

                    self._fast_market_until = now + self.price_velocity_quiesce_seconds
                    self._mid_price_history.clear()
                    await self._cancel_all_quotes()
                    return

        # Check post-fill adverse selection backoff pause
        if self.post_fill_pause_seconds > 0:
            last_fill_time = self.inv_manager.get_last_fill_time(self.ticker)
            if last_fill_time > 0 and (now - last_fill_time) < self.post_fill_pause_seconds:
                remaining = self.post_fill_pause_seconds - (now - last_fill_time)
                logger.info(
                    f"Post-fill adverse selection pause active for {self.ticker} "
                    f"({remaining:.2f}s remaining). Withdrawing quotes."
                )
                if self.current_bid_id or self.current_ask_id:
                    await self._cancel_all_quotes()
                return

        # 4. Calculate Reservation Price (R)
        # In multi-contract dynamic sizing, inventory is normalized by quote lot size:
        # q = inventory / quote_size
        normalized_q = (inventory / quote_size) if (self.order_dollars and quote_size > 0) else inventory
        reservation_price = mid_price - (normalized_q * self.gamma)

        # 5. Determine Quoting Spread (Dynamic Volatility & Fee-Aware Spread vs. Static Fallback)
        if self.dynamic_spread_enabled:
            self.volatility_estimator.update(mid_price, timestamp=now)
            realized_vol = self.volatility_estimator.get_realized_volatility(now=now)
            book_depth = 1
            if best_bid and len(best_bid) > 1 and isinstance(best_bid[1], (int, float)):
                book_depth += int(best_bid[1])
            if best_ask and len(best_ask) > 1 and isinstance(best_ask[1], (int, float)):
                book_depth += int(best_ask[1])

            effective_spread = calculate_optimal_spread(
                mid_price_cents=mid_price,
                realized_volatility_cents=realized_vol,
                gamma=self.gamma,
                book_depth=book_depth,
                target_edge_cents=self.target_edge_cents,
                fee_rate=self.kalshi_fee_rate,
                min_spread_cents=self.min_dynamic_spread_cents,
                max_spread_cents=self.max_dynamic_spread_cents,
            )
            spread_log_info = f"Spread={effective_spread}c (Vol={realized_vol:.2f}c)"
        else:
            effective_spread = self.min_spread
            spread_log_info = f"Spread={effective_spread}c"
        
        # 6. Calculate Optimal Bid/Ask
        optimal_bid = math.floor(reservation_price - (effective_spread / 2.0))
        optimal_ask = math.ceil(reservation_price + (effective_spread / 2.0))
        
        # Ensure prices fit within Kalshi bounds (1c to 99c)
        optimal_bid = max(1, min(optimal_bid, 98))
        optimal_ask = max(2, min(optimal_ask, 99))
        
        # Ensure we don't cross the spread against ourselves
        if optimal_bid >= optimal_ask:
            optimal_bid = optimal_ask - 1
            
        # Active Inventory Mitigation:
        # If inventory is skewed too far, stop quoting the skew direction and aggressively cross/tighten on the other.
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
                f"ReservationPrice={reservation_price:.2f}c | {spread_log_info} "
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
        now = time.time()
        is_hedge = (new_bid is None and new_ask is not None) or (new_ask is None and new_bid is not None)
        in_error_cooldown = (now - self._last_order_error_time < self._order_error_cooldown) and not is_hedge

        if bid_needs_update and not in_error_cooldown:
            if new_bid is not None and self.current_bid_id is None:
                logger.info(f">> Placing new BID: {target_bid_size} YES @ {new_bid}c (Shard {self.exchange_index})")
                order_kwargs = {"exchange_index": self.exchange_index} if getattr(self, "exchange_index", 0) else {}
                self.current_bid_id = await self.om.place_order(
                    ticker=self.ticker,
                    side="yes",
                    action="buy",
                    count=target_bid_size,
                    price=new_bid,
                    **order_kwargs,
                )
                if self.current_bid_id:
                    self.current_bid_price = new_bid
                    self.current_bid_size = target_bid_size
                    if is_hedge:
                        self._consecutive_hedge_failures = 0
                else:
                    self._last_order_error_time = time.time()
                    self.current_bid_price = None
                    self.current_bid_size = None
                    if is_hedge:
                        self._consecutive_hedge_failures += 1
                        logger.warning(
                            f"Hedge BID order placement failed (failure count: {self._consecutive_hedge_failures}/3). "
                            f"Attempting shard rebalance..."
                        )
                        hedge_dollars = max(20.0, (float(target_bid_size * new_bid) / 100.0) + 5.0)
                        await self.ensure_shard_collateral(min_required_dollars=hedge_dollars)
                        if self._consecutive_hedge_failures >= 3:
                            logger.critical(
                                f"Hedge circuit breaker tripped: 3 consecutive hedge order failures on {self.ticker} "
                                f"(Shard {self.exchange_index}). Tripping synchronous kill switch."
                            )
                            from execution.kill_switch import KillSwitch
                            killer = KillSwitch(self.om, ticker=self.ticker)
                            await asyncio.to_thread(killer.trigger_synchronous, ticker=self.ticker)
                            await send_alert(
                                f"🚨 CRITICAL: Stop-loss hedge order placement failed {self._consecutive_hedge_failures} "
                                f"consecutive times on {self.ticker} (Shard {self.exchange_index}). Emergency kill switch activated."
                            )
                            self._market_inactive = True

        # Place new ASK if needed
        if ask_needs_update and not in_error_cooldown:
            if new_ask is not None and self.current_ask_id is None:
                logger.info(f">> Placing new ASK: {target_ask_size} YES @ {new_ask}c (Shard {self.exchange_index})")
                order_kwargs = {"exchange_index": self.exchange_index} if getattr(self, "exchange_index", 0) else {}
                self.current_ask_id = await self.om.place_order(
                    ticker=self.ticker,
                    side="yes",
                    action="sell",
                    count=target_ask_size,
                    price=new_ask,
                    **order_kwargs,
                )
                if self.current_ask_id:
                    self.current_ask_price = new_ask
                    self.current_ask_size = target_ask_size
                    if is_hedge:
                        self._consecutive_hedge_failures = 0
                else:
                    self._last_order_error_time = time.time()
                    self.current_ask_price = None
                    self.current_ask_size = None
                    if is_hedge:
                        self._consecutive_hedge_failures += 1
                        logger.warning(
                            f"Hedge ASK order placement failed (failure count: {self._consecutive_hedge_failures}/3). "
                            f"Attempting shard rebalance..."
                        )
                        hedge_dollars = max(20.0, (float(target_ask_size * new_ask) / 100.0) + 5.0)
                        await self.ensure_shard_collateral(min_required_dollars=hedge_dollars)
                        if self._consecutive_hedge_failures >= 3:
                            logger.critical(
                                f"Hedge circuit breaker tripped: 3 consecutive hedge order failures on {self.ticker} "
                                f"(Shard {self.exchange_index}). Tripping synchronous kill switch."
                            )
                            from execution.kill_switch import KillSwitch
                            killer = KillSwitch(self.om, ticker=self.ticker)
                            await asyncio.to_thread(killer.trigger_synchronous, ticker=self.ticker)
                            await send_alert(
                                f"🚨 CRITICAL: Stop-loss hedge order placement failed {self._consecutive_hedge_failures} "
                                f"consecutive times on {self.ticker} (Shard {self.exchange_index}). Emergency kill switch activated."
                            )
                            self._market_inactive = True



    async def liquidate_inventory(self, ticker: Optional[str] = None, max_retries: int = 3) -> bool:
        """
        Aggressively liquidates open inventory for a ticker by crossing the orderbook
        spread in slices up to self.max_order_contracts until position is flat (0).
        Cancels resting quotes before placing liquidation orders.
        Returns True if inventory is confirmed flat (0), False otherwise.
        """
        target_ticker = ticker or self.ticker
        current_inv = self.inv_manager.get_position(target_ticker)
        if current_inv == 0:
            logger.info(f"Inventory for {target_ticker} is already flat (0). No liquidation needed.")
            return True

        logger.warning(
            f"Initiating aggressive liquidation for {target_ticker}: "
            f"net position = {current_inv} contracts."
        )

        # Cancel any active quotes for target ticker
        if not await self._cancel_all_quotes(ticker=target_ticker):
            logger.error(
                f"Failed to cancel all quotes for {target_ticker}; "
                f"aborting liquidation to prevent racing orders."
            )
            return False

        failed_attempts = 0
        total_slices = 0
        while failed_attempts < max_retries:
            inv = self.inv_manager.get_position(target_ticker)
            if inv == 0:
                logger.info(f"Liquidation confirmed for {target_ticker}. Net position is 0.")
                return True

            best_bid = self.ob_manager.get_best_bid(target_ticker)
            best_ask = self.ob_manager.get_best_ask(target_ticker)

            if (inv > 0 and not best_bid) or (inv < 0 and not best_ask):
                logger.error(
                    f"No opposing liquidity on {target_ticker} for liquidation (position={inv}); "
                    f"refusing to place an unprotected 1c/99c order."
                )
                return False

            slice_count = min(abs(inv), self.max_order_contracts)
            if inv > 0:
                action = "sell"
                # Cross spread to sell YES: floor the bid so the limit order never sits above it
                price = max(1, min(math.floor(best_bid[0]), 99))
            else:
                action = "buy"
                # Cross spread to buy YES: ceil the ask so the limit order never sits below it
                price = max(1, min(math.ceil(best_ask[0]), 99))

            total_slices += 1
            logger.warning(
                f"[LIQUIDATION SLICE {total_slices}] Placing {action.upper()} {slice_count} YES "
                f"@ {price}c for {target_ticker} (current position: {inv}, consecutive failed attempts: {failed_attempts}/{max_retries})"
            )

            order_kwargs = {}
            if getattr(self, "exchange_index", 0) and target_ticker == self.ticker:
                order_kwargs["exchange_index"] = self.exchange_index
            order_id = await self.om.place_order(
                ticker=target_ticker,
                side="yes",
                action=action,
                count=slice_count,
                price=price,
                **order_kwargs,
            )
            if not order_id:
                logger.error(
                    f"Failed to place liquidation order on {target_ticker}; "
                    f"order outcome unconfirmed. Aborting liquidation to avoid untracked exposure."
                )
                return False

            # Wait briefly for execution / WebSocket fill propagation
            await asyncio.sleep(0.5)

            # Clean up any un-executed portion if order is still resting
            if order_id in self.om.active_orders:
                if not await self.om.cancel_order(order_id):
                    logger.error(
                        f"Failed to cancel liquidation order {order_id} on {target_ticker}; "
                        f"aborting to prevent overfill or unintended position exposure."
                    )
                    return False

            try:
                recon_res = await self.om.reconcile_resting_orders(ticker=target_ticker)
                if recon_res is None:
                    logger.error(
                        f"Resting-order reconciliation failed during liquidation on {target_ticker}; "
                        f"aborting to prevent overfill."
                    )
                    return False
            except Exception as e_rec:
                logger.error(f"Resting-order reconciliation raised during liquidation on {target_ticker}: {e_rec}")
                return False

            # Drain any pending WebSocket fill tasks from the event loop before sizing next slice
            await asyncio.sleep(0.1)

            new_inv = self.inv_manager.get_position(target_ticker)

            # Authoritative Execution and Fill Barrier:
            # If local inventory has not changed, verify execution on exchange before sizing next slice.
            # Prevents submitting duplicate slices if WebSocket fill notifications are delayed.
            if new_inv == inv and hasattr(self.om, "get_order_status"):
                order_status = await self.om.get_order_status(order_id)
                if order_status is not None:
                    count_raw = order_status.get(
                        "initial_count_fp",
                        order_status.get("count_fp", order_status.get("count", slice_count)),
                    )
                    rem_raw = order_status.get(
                        "remaining_count_fp",
                        order_status.get("remaining_count"),
                    )
                    fill_raw = order_status.get(
                        "fill_count_fp",
                        order_status.get("fill_count"),
                    )
                    status_str = order_status.get("status")

                    try:
                        if fill_raw is not None:
                            exec_count = max(Decimal("0"), Decimal(str(fill_raw)))
                        elif rem_raw is not None:
                            exec_count = max(
                                Decimal("0"),
                                Decimal(str(count_raw)) - Decimal(str(rem_raw)),
                            )
                        elif status_str == "executed":
                            exec_count = Decimal(str(count_raw))
                        else:
                            exec_count = Decimal("0")
                    except (InvalidOperation, TypeError, ValueError) as exc:
                        logger.error(
                            f"Invalid execution quantities for order {order_id} on {target_ticker}: {exc}; "
                            f"aborting liquidation to prevent unverified exposure."
                        )
                        return False

                    if exec_count > 0:
                        logger.info(
                            f"Order {order_id} executed {exec_count} contracts on exchange; "
                            f"awaiting WebSocket fill processing..."
                        )
                        # Wait up to 2 seconds for WebSocket fill to be dispatched locally
                        for _ in range(20):
                            await asyncio.sleep(0.1)
                            new_inv = self.inv_manager.get_position(target_ticker)
                            if new_inv != inv:
                                break

                        # If WebSocket fill still hasn't arrived after wait, perform authoritative REST hydration
                        if new_inv == inv:
                            logger.warning(
                                f"WebSocket fill delayed for order {order_id} on {target_ticker}; "
                                f"performing authoritative REST position hydration..."
                            )
                            hydrated = await self.inv_manager.hydrate(is_startup=False)
                            new_inv = self.inv_manager.get_position(target_ticker)
                            if new_inv == inv:
                                logger.error(
                                    f"Authoritative position confirmation failed for {target_ticker} after order {order_id}; "
                                    f"aborting liquidation to prevent unverified exposure."
                                )
                                return False
                else:
                    # Order status query failed; attempt authoritative position hydration as fallback
                    logger.warning(
                        f"Order status query unconfirmed for {order_id}; "
                        f"attempting authoritative REST position check for {target_ticker}..."
                    )
                    hydrated = await self.inv_manager.hydrate(is_startup=False)
                    new_inv = self.inv_manager.get_position(target_ticker)
                    if not hydrated and new_inv == inv:
                        logger.error(
                            f"Execution outcome of liquidation order {order_id} unconfirmed on {target_ticker}; "
                            f"aborting liquidation to prevent overfill."
                        )
                        return False

            if (inv > 0 and new_inv < 0) or (inv < 0 and new_inv > 0):
                logger.error(
                    f"Liquidation position flipped from {inv} to {new_inv} on {target_ticker}; "
                    f"aborting to prevent oscillation."
                )
                return False

            if new_inv == 0:
                logger.info(f"Liquidation confirmed for {target_ticker}. Net position is 0.")
                return True

            if abs(new_inv) < abs(inv):
                failed_attempts = 0
            else:
                failed_attempts += 1
                logger.warning(
                    f"Liquidation slice {total_slices} on {target_ticker} did not reduce position "
                    f"(position remains {new_inv}). Consecutive failed attempts: {failed_attempts}/{max_retries}."
                )

        # Final position verification
        remaining_inv = self.inv_manager.get_position(target_ticker)
        if remaining_inv == 0:
            logger.info(f"Liquidation successfully flattened inventory for {target_ticker}.")
            return True
        else:
            logger.error(
                f"Liquidation incomplete for {target_ticker} after {total_slices} slices and "
                f"{failed_attempts} failed attempts. Remaining inventory: {remaining_inv} contracts."
            )
            return False

    async def _cancel_all_quotes(self, ticker: Optional[str] = None) -> bool:
        """Withdraws all active quotes from the market."""
        target_ticker = ticker or self.ticker
        tasks = []
        cancel_targets = []
        if target_ticker == self.ticker:
            if self.current_bid_id:
                tasks.append(self.om.cancel_order(self.current_bid_id))
                cancel_targets.append(("bid", self.current_bid_id))
            if self.current_ask_id:
                tasks.append(self.om.cancel_order(self.current_ask_id))
                cancel_targets.append(("ask", self.current_ask_id))
             
        all_cancelled = True

        if tasks:
            logger.info(f"Withdrawing quotes for {target_ticker}...")
            results = await asyncio.gather(*tasks, return_exceptions=True)

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

        # Actively sweep any resting orders on the exchange for this market ticker
        try:
            recon_res = await self.om.reconcile_resting_orders(ticker=target_ticker)
            if recon_res is None:
                logger.error(f"Resting-order reconciliation failed for {target_ticker} during quote withdrawal.")
                all_cancelled = False
        except Exception as e_rec:
            logger.error(f"Exchange resting order sweep failed during quote withdrawal: {e_rec}")
            all_cancelled = False

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
            killer = KillSwitch(self.om, ticker=self.ticker)
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

    async def rotate_market(self, new_ticker: str, allow_liquidation: bool = True) -> bool:
        """
        Dynamically rotate bot quoting and orderbook subscription to a new market ticker
        without restarting the container process.

        Args:
            new_ticker: The target ticker to switch quoting to.
            allow_liquidation: Whether to liquidate existing inventory if detected. For scheduled
                slate rotations, set to False so an in-flight fill during quote cancellation
                aborts the rotation and retains the position for standard market-making unwinds.
        """
        old_ticker = self.ticker
        if new_ticker == old_ticker:
            return True

        logger.warning(f"Rotating target market from {old_ticker} to {new_ticker}...")

        # 1. Withdraw all active quotes on the previous ticker and ensure no active orders remain
        quotes_cancelled = await self._cancel_all_quotes()
        reconciliation_succeeded = False
        try:
            recon_res = await self.om.reconcile_resting_orders(ticker=old_ticker)
            reconciliation_succeeded = (recon_res is not None)
        except Exception as e_rec:
            logger.error(f"Exchange resting order sweep failed during market rotation: {e_rec}")
            reconciliation_succeeded = False

        if not reconciliation_succeeded:
            logger.error(
                f"Aborting market rotation from {old_ticker} to {new_ticker}; "
                f"resting-order reconciliation failed or was unconfirmed."
            )
            await self._escalate_to_kill_switch(context=f"resting-order reconciliation failure during rotation from {old_ticker}")
            return False

        has_active_orders = bool(self.current_bid_id or self.current_ask_id or self.om.active_orders)
        if not quotes_cancelled or has_active_orders:
            logger.error(
                f"Aborting market rotation from {old_ticker} to {new_ticker}; "
                f"active orders remain or quote cancellation was not confirmed."
            )
            await self._escalate_to_kill_switch(context=f"market rotation from {old_ticker} to {new_ticker}")
            return False

        # 1b. Liquidate any open inventory on old_ticker before unsubscribing
        old_inv = self.inv_manager.get_position(old_ticker)
        if old_inv != 0:
            if not allow_liquidation:
                logger.warning(
                    f"Open inventory of {old_inv} contracts detected on {old_ticker} during rotation "
                    f"with allow_liquidation=False. Aborting rotation to manage existing position."
                )
                return False
            logger.warning(
                f"Open inventory of {old_inv} contracts detected on {old_ticker} during rotation. "
                f"Liquidating position to prevent unhedged inventory decay..."
            )
            liquidation_ok = await self.liquidate_inventory(ticker=old_ticker)
            if not liquidation_ok:
                # Trigger immediate portfolio REST hydration to reconcile potential exchange settlement
                hydration_succeeded = False
                try:
                    hydration_succeeded = await self.inv_manager.hydrate(is_startup=False)
                except Exception as e_hyd:
                    logger.error(f"Failed to hydrate inventory during rotation from {old_ticker}: {e_hyd}")

                remaining_inv = self.inv_manager.get_position(old_ticker)
                if hydration_succeeded and remaining_inv == 0:
                    logger.info(
                        f"Inventory for {old_ticker} reconciled to 0 following exchange hydration; proceeding with rotation."
                    )
                else:
                    raw_status = await get_raw_market_status_async(old_ticker)
                    if raw_status in ("closed", "settled", "determined", "finalized"):
                        logger.warning(
                            f"Market {old_ticker} is {raw_status} on exchange with unliquidated inventory {remaining_inv}. "
                            f"Proceeding with rotation to {new_ticker} as exchange settlement will finalize position."
                        )
                        await send_alert(
                            f"⚠️ Market {old_ticker} is {raw_status} with remaining position {remaining_inv}. "
                            f"Rotating to {new_ticker}; awaiting exchange settlement."
                        )
                    else:
                        logger.error(
                            f"Aborting rotation from {old_ticker} to {new_ticker}: failed to liquidate existing inventory. "
                            f"Remaining inventory: {remaining_inv} (market status: {raw_status})"
                        )
                        await send_alert(
                            f"⚠️ Rotation aborted: liquidation of {old_ticker} incomplete. "
                            f"Remaining position: {remaining_inv} (market status: {raw_status})."
                        )
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
        self.exchange_index = await asyncio.to_thread(get_market_exchange_index, new_ticker)
        self._consecutive_hedge_failures = 0
        self._starvation_start_time = None
        self._starvation_alert_sent = False
        self._last_empty_ob_log = 0.0
        self._last_market_status_check = time.time()
        self._market_inactive = False
        self._last_pnl_snapshot = time.time()
        self._mid_price_history.clear()
        if hasattr(self, "volatility_estimator"):
            self.volatility_estimator.reset()
        self._fast_market_until = 0.0
        self._market_attached_time = time.time()
        self._last_slate_check = time.time()

        # Start a new tracking session for the new market ticker
        self.inv_manager.pnl_tracker.reset_market_session(new_ticker)

        # 4. Ensure collateral on new shard before quoting commences
        await self.ensure_shard_collateral()

        logger.info(f"Market rotation complete. Now trading {new_ticker} on Shard {self.exchange_index}.")
        await send_alert(f"Market Rotated: Switched target from {old_ticker} to active market {new_ticker}.")
        return True

    async def _maybe_recheck_slate(self, now: float) -> bool:
        """
        Periodically re-evaluate whether the currently quoted market is off-slate or distant,
        and rotate to a higher-priority, imminent live game if available.
        
        Invariants:
        - Only executes when inventory == 0 (zero-inventory gating).
        - Enforces minimum market dwell time (e.g. 30 mins) to prevent order churn and preserve queue priority.
        - Does not rotate if current market is already in its active pre-game window (<= 18h)
          and in the current top-priority in-season league.
        - Operator-targeted exact markets bypass automatic slate re-evaluation.
        """
        # Check if operator targeted an exact non-sports market or explicit ticker
        pref = (self.target_preference or "").strip().upper()
        is_sports_pref = pref in (
            "NFL", "FOOTBALL", "NBA", "BASKETBALL", "MLB", "BASEBALL",
            "CFB", "NCAAF", "COLLEGE FOOTBALL",
            "SPORTS", "SPORT", "MAJOR SPORTS"
        ) or not pref
        if not is_sports_pref and pref == self.ticker.upper():
            return False

        # 1. Inspect current market's event horizon and league
        current_details = await get_market_details_async(self.ticker)
        if not current_details:
            return False

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        _, current_close_dt = _get_earliest_market_timestamp(current_details)

        current_hours = 999.0
        if current_close_dt is not None:
            current_hours = max(0.0, (current_close_dt - now_utc).total_seconds() / 3600.0)

        # Check calendar-driven in-season league priority (offloaded to thread to avoid event-loop blocking)
        in_season_leagues = await SportsSeasonRouter.get_in_season_leagues_async(now_utc)
        primary_league = in_season_leagues[0] if in_season_leagues else ""
        current_in_primary = bool(primary_league and self.ticker.startswith(f"KX{primary_league}"))

        # If current market is already in its active imminent window (<= 18h) AND in primary league, stay!
        if current_hours <= 18.0 and current_in_primary:
            return False

        # Current market is distant (> 18h) or not in the primary in-season league.
        # Probe market discovery for an active imminent candidate with a bounded timeout
        try:
            replacement = await asyncio.wait_for(
                discover_active_market_async(
                    target_preference=self.target_preference,
                    exclude_tickers=[self.ticker],
                    min_mid_price=self.min_mid_price,
                    max_mid_price=self.max_mid_price,
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[SLATE ROTATION] Discovery timed out while probing replacements for {self.ticker}; "
                f"retaining current market."
            )
            return False
        except Exception as e:
            logger.error(f"[SLATE ROTATION] Discovery error while probing replacements: {e}")
            return False

        if not replacement or replacement == self.ticker:
            return False

        # Verify replacement market details
        repl_details = await get_market_details_async(replacement)
        if not repl_details:
            return False

        _, repl_close_dt = _get_earliest_market_timestamp(repl_details)
        repl_hours = 999.0
        if repl_close_dt is not None:
            repl_hours = max(0.0, (repl_close_dt - now_utc).total_seconds() / 3600.0)

        repl_in_primary = bool(primary_league and replacement.startswith(f"KX{primary_league}"))

        # Ensure replacement is strictly superior:
        # 1) Replacement is in the active imminent pre-game window (<= 18h) and current is not, OR
        # 2) Replacement is in the primary league while current is not, OR
        # 3) Both are in primary league, but replacement kicks off significantly sooner (e.g. >= 12h sooner).
        is_superior = False
        if repl_hours <= 18.0 and current_hours > 18.0:
            is_superior = True
        elif repl_in_primary and not current_in_primary:
            is_superior = True
        elif repl_in_primary == current_in_primary and (current_hours - repl_hours) >= 12.0:
            is_superior = True

        if not is_superior:
            return False

        # Check inventory again immediately before rotating!
        # If a fill occurred while awaiting discovery REST calls, abort rotation to avoid adverse liquidation
        latest_inv = self.inv_manager.get_position(self.ticker)
        if latest_inv != 0:
            logger.warning(
                f"[SLATE ROTATION] Aborting rotation to {replacement}: position changed to {latest_inv} "
                f"during discovery evaluation for {self.ticker}."
            )
            return False

        logger.info(
            f"[SLATE ROTATION] Scheduled re-evaluation found superior market {replacement} "
            f"(event in {repl_hours:.1f}h, primary={repl_in_primary}) vs current {self.ticker} "
            f"(event in {current_hours:.1f}h, primary={current_in_primary}). Rotating..."
        )
        return await self.rotate_market(replacement, allow_liquidation=False)

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

        # 1b. Liquidate any open inventory on shutdown
        if self.inv_manager.get_position(self.ticker) != 0:
            logger.warning(f"Liquidating remaining inventory on shutdown for {self.ticker}...")
            if not await self.liquidate_inventory(self.ticker):
                logger.error(f"Failed to fully liquidate inventory on {self.ticker} during shutdown.")
                quotes_cancelled = False

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


# Backward-compatible alias
MarketMaker = AvellanedaStoikovBot
