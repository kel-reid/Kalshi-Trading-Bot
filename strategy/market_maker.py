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
from typing import Optional, Union, List

from data.websocket_client import KalshiWebsocketClient, FatalIngestionError
from data.orderbook_manager import OrderbookManager
from data.inventory_manager import InventoryManager
from data.fill_store import FillStore
from data.fill_sync import FillSync
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
    MAX_LOW_PRICE_CONTRACTS,
    LOW_PRICE_THRESHOLD_CENTS,
    MOMENTUM_SKEW_ENABLED,
    MOMENTUM_GAMMA_FACTOR,
    MAX_GAMMA_MULTIPLIER,
    AUTO_SWEEP_SHARD0_ENABLED,
    AUTO_SWEEP_REVERSE_ENABLED,
    SHARD0_RESERVE_DOLLARS,
    SHARD_RETAIN_DOLLARS,
    QUOTE_REPLACEMENT_TOLERANCE_CENTS,
    BLACKOUT_ENABLED,
    EXPIRATION_BLACKOUT_SECONDS,
    BLACKOUT_DANGER_PRICE_LOW,
    BLACKOUT_DANGER_PRICE_HIGH,
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
        max_low_price_contracts: Optional[int] = None,
        low_price_threshold_cents: Optional[int] = None,
        momentum_skew_enabled: Optional[bool] = None,
        momentum_gamma_factor: Optional[float] = None,
        max_gamma_multiplier: Optional[float] = None,
        auto_sweep_shard0_enabled: Optional[bool] = None,
        auto_sweep_reverse_enabled: Optional[bool] = None,
        shard0_reserve_dollars: Optional[float] = None,
        shard_retain_dollars: Optional[float] = None,
        quote_replacement_tolerance_cents: Optional[int] = None,
        blackout_enabled: Optional[bool] = None,
        expiration_blackout_seconds: Optional[int] = None,
        blackout_danger_price_low: Optional[int] = None,
        blackout_danger_price_high: Optional[int] = None,
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

        # Collateral-constrained sizing ceiling for low-price contracts
        self.max_low_price_contracts = (
            max(0, int(max_low_price_contracts))
            if max_low_price_contracts is not None
            else MAX_LOW_PRICE_CONTRACTS
        )
        self.low_price_threshold_cents = (
            max(1, int(low_price_threshold_cents))
            if low_price_threshold_cents is not None
            else LOW_PRICE_THRESHOLD_CENTS
        )

        # Aggressive momentum quote skewing
        self.momentum_skew_enabled = (
            bool(momentum_skew_enabled)
            if momentum_skew_enabled is not None
            else MOMENTUM_SKEW_ENABLED
        )
        self.momentum_gamma_factor = (
            max(0.0, float(momentum_gamma_factor))
            if momentum_gamma_factor is not None
            else MOMENTUM_GAMMA_FACTOR
        )
        self.max_gamma_multiplier = max(
            1.0,
            float(max_gamma_multiplier) if max_gamma_multiplier is not None else float(MAX_GAMMA_MULTIPLIER),
        )

        # Automated inter-shard capital sweep (Bidirectional)
        self.auto_sweep_shard0_enabled = (
            bool(auto_sweep_shard0_enabled)
            if auto_sweep_shard0_enabled is not None
            else AUTO_SWEEP_SHARD0_ENABLED
        )
        self.auto_sweep_reverse_enabled = (
            bool(auto_sweep_reverse_enabled)
            if auto_sweep_reverse_enabled is not None
            else AUTO_SWEEP_REVERSE_ENABLED
        )
        self.shard0_reserve_dollars = (
            max(0.0, float(shard0_reserve_dollars))
            if shard0_reserve_dollars is not None
            else SHARD0_RESERVE_DOLLARS
        )
        self.shard_retain_dollars = (
            max(0.0, float(shard_retain_dollars))
            if shard_retain_dollars is not None
            else SHARD_RETAIN_DOLLARS
        )

        # Quote Replacement Tolerance
        self.quote_replacement_tolerance_cents = (
            max(0, int(quote_replacement_tolerance_cents))
            if quote_replacement_tolerance_cents is not None
            else QUOTE_REPLACEMENT_TOLERANCE_CENTS
        )

        # Trading Blackout Safeguards (Late-Game & Collar Approach)
        self.blackout_enabled = (
            bool(blackout_enabled)
            if blackout_enabled is not None
            else BLACKOUT_ENABLED
        )
        self.expiration_blackout_seconds = (
            max(0, int(expiration_blackout_seconds))
            if expiration_blackout_seconds is not None
            else EXPIRATION_BLACKOUT_SECONDS
        )
        self.blackout_danger_price_low = (
            max(1, int(blackout_danger_price_low))
            if blackout_danger_price_low is not None
            else BLACKOUT_DANGER_PRICE_LOW
        )
        self.blackout_danger_price_high = (
            min(99, int(blackout_danger_price_high))
            if blackout_danger_price_high is not None
            else BLACKOUT_DANGER_PRICE_HIGH
        )
        self._market_close_time: Optional[datetime.datetime] = None
        self._in_expiration_blackout: bool = False


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
        self.inv_manager.fill_store = FillStore(self.om.db_pool)
        self.fill_store = self.inv_manager.fill_store
        
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
        self._ws_task: Optional[asyncio.Task] = None
        self._strategy_task: Optional[asyncio.Task] = None
        self._ingestion_failed = False
        self._stopping = False
        self._fill_connection_generation = None
        self._sync_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None

        # Exchange Shard Routing & Hedging Circuit Breaker
        self.exchange_index: Optional[int] = get_market_exchange_index(self.ticker)
        self._consecutive_hedge_failures: int = 0
        self._last_shard_rebalance_time: float = 0.0
        self._last_shard_rebalance_dispatch_time: float = 0.0

    def request_stop(self) -> None:
        """Signal that the bot is stopping gracefully, coordinating shutdown across tasks."""
        self._stopping = True
        self.running = False

    def _is_inventory_flat(self, ticker: Optional[str] = None) -> bool:
        """Returns True only for an exactly flat local position."""
        target_ticker = ticker or self.ticker
        pos = self.inv_manager.get_position(target_ticker)
        return pos == 0

    def _get_rotation_exclude_tickers(self, exclude_event: bool = False) -> List[str]:
        """
        Builds the list of tickers to exclude during market rotation.
        If exclude_event is True and ticker has a multi-segment event prefix (e.g. KXNBAGAME-26OCT28PHIIND-PHI),
        also excludes the event prefix (KXNBAGAME-26OCT28PHIIND) to prevent rotating into opposing teams or
        game lines of the same expiring/closed event.
        """
        exclude = [self.ticker]
        if exclude_event and self.ticker and self.ticker.count("-") >= 2:
            event_prefix = self.ticker.rsplit("-", 1)[0]
            exclude.append(event_prefix)
        return exclude

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

            total_account_balance = float(balances.get("total_balance_dollars", current_shard_balance + shard0_balance))
            available_sweep = max(0.0, round(shard0_balance - self.shard0_reserve_dollars, 2))
            available_funds = round(current_shard_balance + available_sweep, 2)

            order_capital_floor = max(1.0, float(self.order_dollars)) if (self.order_dollars is not None and math.isfinite(self.order_dollars)) else 1.0
            req_dollars = float(min_required_dollars) if math.isfinite(min_required_dollars) else 15.0
            min_target_floor = min(req_dollars, order_capital_floor)

            if available_funds < min_target_floor:
                logger.error(
                    f"Available funds (${available_funds:.2f}) across Shard {target_shard} (${current_shard_balance:.2f}) "
                    f"and sweepable Shard 0 (${available_sweep:.2f}) are insufficient to fund minimum order capital "
                    f"(${min_target_floor:.2f}, ORDER_DOLLARS=${order_capital_floor:.2f}) for {self.ticker}."
                )
                return False

            effective_min_dollars = min(req_dollars, max(min_target_floor, available_funds))

            needed_dollars = round(effective_min_dollars - current_shard_balance, 2)

            if current_shard_balance >= effective_min_dollars:
                # Target shard already meets minimum required collateral.
                # If auto-sweep is enabled, sweep idle capital from Shard 0 to active sports shard
                if self.auto_sweep_shard0_enabled and available_sweep >= 1.0:
                    logger.info(
                        f"Shard {target_shard} funded (${current_shard_balance:.2f} >= ${effective_min_dollars:.2f}), "
                        f"but idle capital detected in Shard 0 (${shard0_balance:.2f}). "
                        f"Initiating automated sweep of ${available_sweep:.2f} to Shard {target_shard}."
                    )
                    transfer_amount = available_sweep
                elif self.auto_sweep_reverse_enabled and target_shard != 0:
                    # Check if target shard has excessive surplus above effective_min + retain
                    surplus = round(current_shard_balance - (effective_min_dollars + self.shard_retain_dollars), 2)
                    if surplus >= 1.0:
                        logger.info(
                            f"Surplus capital detected in Shard {target_shard} (${current_shard_balance:.2f} > "
                            f"${effective_min_dollars:.2f} + ${self.shard_retain_dollars:.2f}). "
                            f"Initiating automated reverse sweep of ${surplus:.2f} to Shard 0."
                        )
                        await asyncio.to_thread(
                            self.om.transfer_intra_exchange,
                            source_shard=target_shard,
                            destination_shard=0,
                            amount_dollars=surplus,
                        )
                    return True
                else:
                    return True
            else:
                if needed_dollars <= 0.0:
                    return True

                logger.warning(
                    f"Shard {target_shard} balance (${current_shard_balance:.2f}) below required "
                    f"${effective_min_dollars:.2f}. Initiating rebalance from Shard 0 (balance: ${shard0_balance:.2f})."
                )

                # Check available transfer amount from Shard 0
                transfer_amount = min(needed_dollars, available_sweep)
                if transfer_amount < 1.0:
                    if needed_dollars < 1.0 and current_shard_balance >= min_target_floor:
                        logger.info(
                            f"Transfer amount (${transfer_amount:.2f}) is below minimum transfer ($1.00), "
                            f"and deficit (${needed_dollars:.2f}) is sub-dollar. Target Shard {target_shard} "
                            f"already holds ${current_shard_balance:.2f} (>= ${min_target_floor:.2f}). Proceeding with existing shard collateral."
                        )
                        return True
                    logger.error(
                        f"Cannot transfer to Shard {target_shard}: transfer amount (${transfer_amount:.2f}) "
                        f"insufficient for minimum transfer ($1.00) and Shard {target_shard} balance (${current_shard_balance:.2f}) "
                        f"is below required ${effective_min_dollars:.2f} (needed: ${needed_dollars:.2f})."
                    )
                    return False

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
                baseline_balance = current_shard_balance
                for attempt in range(6):
                    await asyncio.sleep(2.0)
                    balances = await asyncio.to_thread(self.om.get_portfolio_balances)
                    shard_balances = balances.get("shard_balances", {})
                    current_shard_balance = float(shard_balances.get(target_shard, 0.0))
                    if needed_dollars > 0:
                        if current_shard_balance >= effective_min_dollars:
                            logger.info(
                                f"Confirmed Shard {target_shard} funded: balance=${current_shard_balance:.2f} "
                                f"(attempt {attempt + 1}/6)."
                            )
                            return True
                    else:
                        if current_shard_balance > baseline_balance:
                            logger.info(
                                f"Confirmed idle capital sweep to Shard {target_shard}: balance=${current_shard_balance:.2f} "
                                f"(attempt {attempt + 1}/6)."
                            )
                            return True
                        elif current_shard_balance >= effective_min_dollars and attempt == 5:
                            return True

                if needed_dollars > 0:
                    logger.error(
                        f"Shard {target_shard} balance (${current_shard_balance:.2f}) not funded with required "
                        f"${effective_min_dollars:.2f} after transfer acceptance."
                    )
                    return False
                return True
            else:
                logger.error(f"Failed to fund Shard {target_shard} from Shard 0.")
                return False
        except Exception as e:
            logger.error(f"Exception ensuring shard collateral for Shard {target_shard}: {e}")
            return False

    async def sweep_shard_to_shard0(
        self,
        source_shard: Optional[int] = None,
        retain_dollars: Optional[float] = None,
    ) -> bool:
        """
        Sweeps excess or idle capital from an active trading shard (e.g. Shard 3) back to Shard 0.
        Ensures clearing pool (Shard 0) is replenished after games conclude or on market rotation/shutdown.
        """
        if not self.auto_sweep_reverse_enabled:
            return True

        target_source = source_shard if source_shard is not None else getattr(self, "exchange_index", None)
        if target_source == 0 or target_source is None:
            return True

        retain = retain_dollars if retain_dollars is not None else self.shard_retain_dollars
        try:
            balances = await asyncio.to_thread(self.om.get_portfolio_balances)
            shard_balances = balances.get("shard_balances", {})
            current_source_bal = float(shard_balances.get(target_source, 0.0))

            sweepable = round(current_source_bal - retain, 2)
            if sweepable < 1.0:
                logger.debug(
                    f"No sweepable surplus in Shard {target_source} (${current_source_bal:.2f} <= retain ${retain:.2f} + $1.00)."
                )
                return True

            logger.info(
                f"Initiating reverse sweep of ${sweepable:.2f} from Shard {target_source} (bal: ${current_source_bal:.2f}) "
                f"back to Shard 0 (retaining ${retain:.2f})."
            )
            ok = await asyncio.to_thread(
                self.om.transfer_intra_exchange,
                source_shard=target_source,
                destination_shard=0,
                amount_dollars=sweepable,
            )
            if ok:
                logger.info(f"Reverse sweep of ${sweepable:.2f} from Shard {target_source} to Shard 0 confirmed.")
                return True
            else:
                logger.error(f"Reverse sweep of ${sweepable:.2f} from Shard {target_source} to Shard 0 failed.")
                return False
        except Exception as e:
            logger.error(f"Error performing reverse sweep from Shard {target_source} to Shard 0: {e}")
            return False

    async def _refresh_market_metadata(self):
        """Fetches market details from Kalshi API and updates market close time."""
        try:
            details = await get_market_details_async(self.ticker)
            if details:
                _, close_dt = _get_earliest_market_timestamp(details)
                if close_dt:
                    self._market_close_time = close_dt
                    logger.info(f"Loaded market metadata for {self.ticker}: close_time={self._market_close_time.isoformat()}")
        except Exception as e:
            logger.debug(f"Failed to refresh market metadata for {self.ticker}: {e}")

    def calculate_order_size(self, mid_price: Optional[float]) -> int:
        """
        Calculates the order size (contract count) for the current quoting cycle.
        If order_dollars is configured (>= $1.00), dynamically calculates the contract count
        required to deploy at least order_dollars at the current mid-price.
        Guaranteed to deploy at least $1.00 and never lower.
        Bounds midpoint to Kalshi's supported quoting limits [1.0, 99.0] and enforces
        a hard pre-submission ceiling (self.max_order_contracts) to prevent runaway
        or manipulated contract counts.
        Additionally enforces a collateral-constrained ceiling (self.max_low_price_contracts)
        when contract prices fall below low_price_threshold_cents (< 25c) to prevent
        collateral exhaustion on the short/NO leg.
        Otherwise falls back to fixed order_size (also capped by max_order_contracts).
        """
        if self.order_dollars is not None and math.isfinite(self.order_dollars) and self.order_dollars >= 1.0:
            if isinstance(mid_price, (int, float)) and not isinstance(mid_price, bool) and math.isfinite(mid_price) and mid_price > 0:
                # Bound midpoint to Kalshi's valid exchange quoting limits [1c, 99c]
                # to prevent runaway contract counts on extreme/sub-cent books (e.g. 0.05c -> 2,000 contracts)
                effective_price = max(1.0, min(99.0, float(mid_price)))
                raw_count = max(1, math.ceil((self.order_dollars * 100.0) / effective_price))
                capped_count = min(self.max_order_contracts, raw_count)
                # Collateral-constrained ceiling for low-price contracts
                if self.max_low_price_contracts and self.max_low_price_contracts > 0:
                    if effective_price <= self.low_price_threshold_cents:
                        capped_count = min(capped_count, self.max_low_price_contracts)
                return capped_count
        fallback = self.order_size if (isinstance(self.order_size, int) and not isinstance(self.order_size, bool) and self.order_size > 0) else 1
        return min(self.max_order_contracts, max(1, fallback))



    async def start(self):
        """Initializes infrastructure and starts the main trading loop."""
        if self._ingestion_failed or self._stopping:
            raise FatalIngestionError("Fill ingestion failed; reconcile missed fills before restarting with a new bot.")
        self._ws_task = asyncio.create_task(self.ws_client.connect())
        strategy_task = asyncio.create_task(self._run_strategy())
        self._strategy_task = strategy_task
        try:
            done, _ = await asyncio.wait((self._ws_task, strategy_task), return_when=asyncio.FIRST_COMPLETED)
            if self._ws_task in done:
                if not self._ws_task.cancelled():
                    self._ingestion_failed = True
                    self.running = False
                await self._ws_task
                raise FatalIngestionError("WebSocket ingestion stopped unexpectedly; reconciliation required")
            await strategy_task
        except BaseException:
            if not self._stopping:
                self._ingestion_failed = True
            raise
        finally:
            self.running = False
            if not strategy_task.done():
                strategy_task.cancel()
            await asyncio.gather(strategy_task, return_exceptions=True)
            failure = None
            strategy_failure = None
            if self._ws_task.done() and not self._ws_task.cancelled():
                failure = self._ws_task.exception() or FatalIngestionError(
                    "WebSocket ingestion stopped unexpectedly; reconciliation required"
                )
            if strategy_task.done() and not strategy_task.cancelled():
                strategy_failure = strategy_task.exception()
                if strategy_failure is not None and not self._stopping:
                    failure = strategy_failure
            if failure is None and self._ingestion_failed and not self._stopping:
                try:
                    if not await self._cancel_all_quotes() or self.om.active_orders or self.om._pending_client_ids:
                        await self._escalate_to_kill_switch(context="cancelled strategy")
                except Exception:
                    logger.exception("Failed to withdraw orders after strategy cancellation")
            if failure is not None:
                self._ingestion_failed = True
                logger.critical("Fill ingestion stopped; reconcile missed fills before resuming: %s", failure)
                try:
                    if not await self._cancel_all_quotes() or self.om.active_orders or self.om._pending_client_ids:
                        await self._escalate_to_kill_switch(context="fatal fill ingestion")
                except Exception:
                    logger.exception("Failed to withdraw orders after fatal ingestion failure")
                if failure is strategy_failure or isinstance(failure, FatalIngestionError):
                    raise failure
                raise FatalIngestionError("WebSocket ingestion failed; reconciliation required") from failure

    async def _subscribe_fills_before_hydration(self):
        """Wait for the exchange to acknowledge the fill subscription."""
        responses = {}
        changed = asyncio.Event()
        generation = self.ws_client.connection_generation

        async def on_response(message):
            if message.get("type") in ("subscribed", "error"):
                responses[message.get("id")] = message
                changed.set()

        self.ws_client.add_message_handler(on_response)
        try:
            await self.inv_manager.subscribe()
            subscriptions = [s for s in self.ws_client.active_subscriptions
                             if s.get("params", {}).get("channels") == ["fill"]]
            if not subscriptions:
                raise RuntimeError("fill subscription was not registered")
            subscription_id = subscriptions[-1]["id"]
            async def wait_for_response():
                while subscription_id not in responses:
                    changed.clear()
                    await changed.wait()
                return responses[subscription_id]

            response = await asyncio.wait_for(wait_for_response(), timeout=10)
            if (response.get("type") != "subscribed"
                    or response.get("msg", {}).get("channel") != "fill"
                    or not self.ws_client.is_connected
                    or self.ws_client.connection_generation != generation):
                raise RuntimeError("fill subscription not confirmed on the current connection")
        except Exception as exc:
            raise FatalIngestionError("Fill ingestion startup failed; reconciliation required") from exc
        finally:
            self.ws_client.message_handlers.remove(on_response)

    async def _run_strategy(self):
        logger.info(f"Starting Market Maker for {self.ticker}")
        
        # Start Prometheus Metrics Server
        start_metrics_server(port=8000)

        # 0. Recover state and cancel orphaned orders
        await self.om.sync_and_recover_state()
        
        # 2. Wait for connection to establish
        while not self.ws_client.is_connected:
            await asyncio.sleep(0.1)
            
        logger.info("WebSocket Connected. Preparing startup...")
        
        # 3a. Ensure exchange shard collateral before quoting starts
        if getattr(self, "exchange_index", None) is None:
            self.exchange_index = await asyncio.to_thread(get_market_exchange_index, self.ticker)

        collateral_ok = False
        for attempt in range(3):
            if await self.ensure_shard_collateral():
                collateral_ok = True
                break
            logger.warning(
                f"Shard collateral verification attempt {attempt + 1}/3 failed for {self.ticker}. "
                f"Retrying in 3.0s..."
            )
            await asyncio.sleep(3.0)

        if not collateral_ok:
            logger.critical(f"Failed to ensure exchange shard collateral for {self.ticker}. Aborting startup.")
            await send_alert(f"Startup Aborted: Failed to secure shard collateral for {self.ticker}.")
            self.running = False
            raise RuntimeError(f"Startup failed: Insufficient verified shard collateral for {self.ticker}.")

        await self._refresh_market_metadata()

        fill_count = self.inv_manager._fill_count
        observed_fill = asyncio.Event()

        async def observe_fill(message):
            if message.get("type") == "fill":
                observed_fill.set()

        self.ws_client.message_handlers.insert(0, observe_fill)
        try:
            await self._subscribe_fills_before_hydration()
            generation = self.ws_client.connection_generation
            self._fill_connection_generation = generation
            hydrated = await self.inv_manager.hydrate(is_startup=True)
            if (observed_fill.is_set() or self.inv_manager._fill_count != fill_count
                    or not self.ws_client.is_connected
                    or self.ws_client.connection_generation != generation):
                raise FatalIngestionError("Startup position snapshot raced fill ingestion or connection loss; reconciliation required")
        finally:
            self.ws_client.message_handlers.remove(observe_fill)
        if not hydrated:
            await send_alert(f"Startup Aborted: Failed to hydrate initial portfolio inventory for {self.ticker}.")
            raise RuntimeError(f"Startup hydration failed for {self.ticker}; cannot safely trade without verified position ground truth.")

        # 3b. Launch the periodic reconciliation background task
        self._sync_task = asyncio.create_task(self.inv_manager._sync_loop())
        self._background_tasks.add(self._sync_task)
        self._sync_task.add_done_callback(self._background_tasks.discard)
        
        await self.ob_manager.subscribe([self.ticker])

        if self.inv_manager.fill_store is not None:
            fill_sync = FillSync(self.inv_manager.fill_store, self.ws_client)
            fill_task = asyncio.create_task(fill_sync.run())
            self._background_tasks.add(fill_task)
            fill_task.add_done_callback(self._background_tasks.discard)
        
        logger.info("State hydrated. Beginning quoting loop.")
        self.running = True
        
        # 4. Enter Main Loop
        try:
            while self.running:
                if not self.ws_client.is_connected or self.ws_client.connection_generation != generation:
                    raise FatalIngestionError("WebSocket connection changed; reconciliation required before resuming")
                await self._tick()
                await asyncio.sleep(1) # Re-evaluate every 1 second
        except asyncio.CancelledError:
            logger.info("Market Maker stopped.")
            raise
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
                if not await self._cancel_all_quotes():
                    self._maybe_schedule_pnl_snapshot(now, inventory)
                    return

            if now - self._last_inactive_retry >= self._inactive_retry_interval:
                self._last_inactive_retry = now
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
                    elif not self._is_inventory_flat():
                        await send_alert(
                            f"⚠️ Inactive market liquidation retry failed for {self.ticker} "
                            f"(position={self.inv_manager.get_position(self.ticker)})."
                        )

                if self.auto_rotate:
                    logger.info(f"Retrying market discovery for inactive market {self.ticker}...")
                    replacement = await discover_active_market_async(
                        target_preference=self.target_preference,
                        exclude_tickers=self._get_rotation_exclude_tickers(exclude_event=True),
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
            await self._refresh_market_metadata()
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
                        exclude_tickers=self._get_rotation_exclude_tickers(exclude_event=True),
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
                        if not self._is_inventory_flat():
                            if not await self.liquidate_inventory(self.ticker):
                                hydrated = False
                                try:
                                    hydrated = await self.inv_manager.hydrate(is_startup=False)
                                except Exception as e_hyd:
                                    logger.error(f"Failed to hydrate inventory during cutoff for {self.ticker}: {e_hyd}")
                                if not hydrated or not self._is_inventory_flat():
                                    await send_alert(f"⚠️ Safeguard liquidation for {self.ticker} is unconfirmed.")
                        self._market_inactive = True
                        self._last_inactive_retry = now
                else:
                    if not self._is_inventory_flat():
                        if not await self.liquidate_inventory(self.ticker):
                            hydrated = False
                            try:
                                hydrated = await self.inv_manager.hydrate(is_startup=False)
                            except Exception as e_hyd:
                                logger.error(f"Failed to hydrate inventory during cutoff for {self.ticker}: {e_hyd}")
                            if not hydrated or not self._is_inventory_flat():
                                await send_alert(f"⚠️ Safeguard liquidation for {self.ticker} is unconfirmed.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
                self._maybe_schedule_pnl_snapshot(now, inventory)
                return
            elif is_active is True:
                self._market_inactive = False
            else:
                logger.warning(f"Unable to confirm market status for {self.ticker}; retaining current market.")

        # Periodic active slate and event-horizon re-evaluation (only when inventory is flat and dwell time elapsed)
        if now - self._last_slate_check >= self._slate_check_interval:
            self._last_slate_check = now
            if (
                self.auto_rotate
                and not self._market_inactive
                and self._is_inventory_flat()
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
                            exclude_tickers=self._get_rotation_exclude_tickers(exclude_event=False),
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
                            if not self._is_inventory_flat():
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
                    exclude_tickers=self._get_rotation_exclude_tickers(exclude_event=True),
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
                    if not self._is_inventory_flat():
                        if not await self.liquidate_inventory(self.ticker):
                            await send_alert(f"⚠️ Safeguard liquidation failed after {reason} on {self.ticker}; open position remains unhedged.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
            else:
                if not self._is_inventory_flat():
                    if not await self.liquidate_inventory(self.ticker):
                        await send_alert(f"⚠️ Safeguard liquidation failed after {reason} on {self.ticker}; open position remains unhedged.")
                self._market_inactive = True
                self._last_inactive_retry = now
            return

        # 2a-0. Expiration Blackout Safeguard (Late-Game Quoting Freeze & Unwind)
        # Prevents opening or accumulating inventory during late-game blowouts and final game stages.
        if self.blackout_enabled and self._market_close_time is not None:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            remaining_to_close = (self._market_close_time - now_utc).total_seconds()
            if remaining_to_close <= self.expiration_blackout_seconds:
                self._in_expiration_blackout = True
                logger.warning(
                    f"[EXPIRATION BLACKOUT] Market {self.ticker} within blackout window "
                    f"({remaining_to_close:.0f}s <= {self.expiration_blackout_seconds}s to close). "
                    f"Current inventory: {inventory}."
                )
                try:
                    SAFEGUARD_EVENTS_TOTAL.labels(safeguard="blackout_expiration", ticker=self.ticker).inc()
                except Exception as e_metric:
                    logger.debug(f"Safeguard metric update failed: {e_metric}")

                if not self._is_inventory_flat():
                    await self.liquidate_inventory(self.ticker)
                    return
                if not await self._cancel_all_quotes():
                    logger.error(f"[EXPIRATION BLACKOUT] Quote cancellation unconfirmed for {self.ticker}.")
                    return
                positions = await self._verified_ticker_positions(self.ticker)
                if positions is None:
                    return
                if any(value != 0 for value in positions.values()) or not self._is_inventory_flat():
                    await self.liquidate_inventory(self.ticker)
                    return
                if self._is_inventory_flat():
                    logger.info(f"[EXPIRATION BLACKOUT] Exchange and local inventory are flat for {self.ticker}.")
                    verified_fill_count = self.inv_manager._fill_count
                    if self.auto_rotate:
                        replacement = await discover_active_market_async(
                            target_preference=self.target_preference,
                            exclude_tickers=self._get_rotation_exclude_tickers(exclude_event=True),
                            min_mid_price=self.min_mid_price,
                            max_mid_price=self.max_mid_price,
                        )
                        if replacement:
                            if await self.rotate_market(replacement):
                                self._market_inactive = False
                                self._in_expiration_blackout = False
                                logger.info(f"Successfully rotated from blackout market to {replacement}.")
                            else:
                                return
                            return
                    positions = await self._verified_ticker_positions(self.ticker)
                    if (positions is not None and not any(value != 0 for value in positions.values())
                            and self._is_inventory_flat() and self.inv_manager._fill_count == verified_fill_count):
                        self._market_inactive = True
                        self._last_inactive_retry = now
                    return
            else:
                self._in_expiration_blackout = False

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
                    exclude_tickers=self._get_rotation_exclude_tickers(exclude_event=False),
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
                    if not self._is_inventory_flat():
                        if not await self.liquidate_inventory(self.ticker):
                            await send_alert(f"⚠️ Safeguard liquidation failed for collar-breached market {self.ticker}; open position remains unhedged.")
                    self._market_inactive = True
                    self._last_inactive_retry = now
            else:
                if not self._is_inventory_flat():
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

        effective_gamma = self.gamma
        if self.momentum_skew_enabled and abs(normalized_q) > 1.0:
            excess_q = abs(normalized_q) - 1.0
            gamma_scale = min(self.max_gamma_multiplier, 1.0 + (self.momentum_gamma_factor * excess_q))
            effective_gamma = self.gamma * gamma_scale

        # Adverse Momentum / Drift Skew:
        # If orderbook mid price has drifted adversely against current inventory position,
        # apply an additional directional shift to reservation price to retreat further from toxic flow.
        adverse_drift_shift = 0.0
        if self.momentum_skew_enabled and normalized_q != 0 and len(self._mid_price_history) >= 2:
            start_price = self._mid_price_history[0][1]
            drift = mid_price - start_price
            # If long (q > 0) and price is falling (drift < 0), market is moving against us
            if normalized_q > 0 and drift < 0:
                adverse_drift_shift = abs(drift) * 0.5
            # If short (q < 0) and price is rising (drift > 0), market is moving against us
            elif normalized_q < 0 and drift > 0:
                adverse_drift_shift = -abs(drift) * 0.5

        reservation_price = mid_price - (normalized_q * effective_gamma) - adverse_drift_shift

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
                gamma=effective_gamma,
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

        # Danger-Zone Blackout Safeguard (Binary Collar Approach Protection):
        # In live binary sporting events, when mid_price <= blackout_danger_price_low (e.g. 20c),
        # the probability of settling to $0 is extreme. Suppress BUY YES quotes to prevent
        # accumulating inventory into a blowout wipeout.
        if self.blackout_enabled and mid_price <= self.blackout_danger_price_low and inventory >= 0:
            if optimal_bid is not None:
                logger.info(
                    f"[DANGER-ZONE BLACKOUT] Mid {mid_price:.1f}c <= {self.blackout_danger_price_low}c. "
                    f"Suppressing BUY YES quote to prevent adverse accumulation."
                )
                optimal_bid = None
                try:
                    SAFEGUARD_EVENTS_TOTAL.labels(safeguard="blackout_danger_zone", ticker=self.ticker).inc()
                except Exception:
                    pass

        # Similarly, when mid_price >= blackout_danger_price_high (e.g. 80c),
        # suppress SELL YES quotes to prevent adverse short/NO accumulation.
        if self.blackout_enabled and mid_price >= self.blackout_danger_price_high and inventory <= 0:
            if optimal_ask is not None:
                logger.info(
                    f"[DANGER-ZONE BLACKOUT] Mid {mid_price:.1f}c >= {self.blackout_danger_price_high}c. "
                    f"Suppressing SELL YES quote to prevent adverse short accumulation."
                )
                optimal_ask = None
                try:
                    SAFEGUARD_EVENTS_TOTAL.labels(safeguard="blackout_danger_zone", ticker=self.ticker).inc()
                except Exception:
                    pass

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

        # Check whether quotes need update (price change >= tolerance OR contract size change on active quote)
        bid_price_changed = False
        if new_bid is None and self.current_bid_price is not None:
            bid_price_changed = True
        elif new_bid is not None and self.current_bid_price is None:
            bid_price_changed = True
        elif new_bid is not None and self.current_bid_price is not None:
            bid_delta = abs(new_bid - self.current_bid_price)
            bid_price_changed = bid_delta > 0 and bid_delta >= self.quote_replacement_tolerance_cents

        ask_price_changed = False
        if new_ask is None and self.current_ask_price is not None:
            ask_price_changed = True
        elif new_ask is not None and self.current_ask_price is None:
            ask_price_changed = True
        elif new_ask is not None and self.current_ask_price is not None:
            ask_delta = abs(new_ask - self.current_ask_price)
            ask_price_changed = ask_delta > 0 and ask_delta >= self.quote_replacement_tolerance_cents

        bid_needs_update = bid_price_changed or (new_bid is not None and target_bid_size != self.current_bid_size)
        ask_needs_update = ask_price_changed or (new_ask is not None and target_ask_size != self.current_ask_size)

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
                    elif now - self._last_shard_rebalance_dispatch_time >= 60.0:
                        logger.warning(
                            f"Standard BID order placement failed on {self.ticker} (Shard {self.exchange_index}). "
                            f"Triggering background shard collateral rebalance..."
                        )
                        self._last_shard_rebalance_dispatch_time = now
                        rebal_task = asyncio.create_task(self.ensure_shard_collateral())
                        self._background_tasks.add(rebal_task)
                        rebal_task.add_done_callback(self._background_tasks.discard)

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
                    elif now - self._last_shard_rebalance_dispatch_time >= 60.0:
                        logger.warning(
                            f"Standard ASK order placement failed on {self.ticker} (Shard {self.exchange_index}). "
                            f"Triggering background shard collateral rebalance..."
                        )
                        self._last_shard_rebalance_dispatch_time = now
                        rebal_task = asyncio.create_task(self.ensure_shard_collateral())
                        self._background_tasks.add(rebal_task)
                        rebal_task.add_done_callback(self._background_tasks.discard)



    async def _verified_ticker_positions(self, ticker: str):
        start_fill_count = self.inv_manager._fill_count
        try:
            positions = await asyncio.to_thread(self.inv_manager._fetch_ticker_positions, ticker)
        except Exception as exc:
            logger.error(f"Position verification failed for {ticker}: {exc}")
            return None
        if self.inv_manager._fill_count != start_fill_count:
            logger.warning(f"Fill raced position verification for {ticker}; refusing stale snapshot.")
            return None
        return positions

    async def liquidate_inventory(self, ticker: Optional[str] = None, max_retries: int = 3) -> bool:
        """
        Aggressively liquidates open inventory for a ticker by crossing the orderbook
        spread in slices up to self.max_order_contracts until position is flat (0).
        Cancels resting quotes before placing liquidation orders.
        Returns True if inventory is confirmed flat (0), False otherwise.
        """
        target_ticker = ticker or self.ticker
        current_inv = self.inv_manager.get_position(target_ticker)

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

        if self.om._pending_client_ids:
            logger.error(f"Cannot liquidate {target_ticker} while order placements are pending.")
            return False

        failed_attempts = 0
        total_slices = 0
        while failed_attempts < max_retries:
            if (self.om._pending_client_ids or any(
                    order.get("ticker") == target_ticker for order in self.om.active_orders.values())):
                logger.error(f"Outstanding orders prevent liquidation of {target_ticker}.")
                return False
            positions = await self._verified_ticker_positions(target_ticker)
            if positions is None:
                return False
            exposed = [(index, value) for index, value in positions.items() if value != 0]
            if not exposed:
                if (self.om._pending_client_ids or any(order.get("ticker") == target_ticker
                        for order in self.om.active_orders.values())
                        or self.inv_manager.get_position(target_ticker) != 0):
                    logger.error(f"Exchange is flat but local inventory for {target_ticker} is not; await fill reconciliation before retrying.")
                    return False
                logger.info(f"Liquidation confirmed for {target_ticker}. Net position is flat.")
                return True
            shard, inv = exposed[0]
            before_positions = positions

            best_bid = self.ob_manager.get_best_bid(target_ticker)
            best_ask = self.ob_manager.get_best_ask(target_ticker)

            if (inv > 0 and not best_bid) or (inv < 0 and not best_ask):
                logger.error(
                    f"No opposing liquidity on {target_ticker} for liquidation (position={inv}); "
                    f"refusing to place an unprotected 1c/99c order."
                )
                return False

            slice_count = min(abs(inv), Decimal(str(self.max_order_contracts)))
            if slice_count <= 0:
                logger.error(
                    f"Residual inventory ({inv}) for {target_ticker} cannot be closed with whole-contract "
                    f"orders; liquidation incomplete."
                )
                return False
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

            order_kwargs = {"reduce_only": True, "time_in_force": "immediate_or_cancel", "exchange_index": shard}
            try:
                order_id = await self.om.place_order(
                    ticker=target_ticker,
                    side="yes",
                    action=action,
                    count=slice_count,
                    price=price,
                    **order_kwargs,
                )
            except Exception as exc:
                logger.error(f"Liquidation order failed for {target_ticker}: {exc}")
                await self._verified_ticker_positions(target_ticker)
                return False
            if not order_id:
                logger.error(
                    f"Failed to place liquidation order on {target_ticker}; "
                    f"order outcome unconfirmed. Aborting liquidation to avoid untracked exposure."
                )
                await self._verified_ticker_positions(target_ticker)
                return False

            # Wait briefly for execution / WebSocket fill propagation
            await asyncio.sleep(0.5)

            # Clean up any un-executed portion if order is still resting
            if order_id in self.om.active_orders:
                try:
                    cancelled = await self.om.cancel_order(order_id)
                except Exception as exc:
                    logger.error(f"Could not cancel liquidation order {order_id}: {exc}")
                    cancelled = False
                if not cancelled:
                    logger.error(
                        f"Failed to cancel liquidation order {order_id} on {target_ticker}; "
                        f"aborting to prevent overfill or unintended position exposure."
                    )
                    await self._verified_ticker_positions(target_ticker)
                    return False

            try:
                recon_res = await self.om.reconcile_resting_orders(ticker=target_ticker)
                if recon_res is None:
                    logger.error(
                        f"Resting-order reconciliation failed during liquidation on {target_ticker}; "
                        f"aborting to prevent overfill."
                    )
                    await self._verified_ticker_positions(target_ticker)
                    return False
            except Exception as e_rec:
                logger.error(f"Resting-order reconciliation raised during liquidation on {target_ticker}: {e_rec}")
                await self._verified_ticker_positions(target_ticker)
                return False

            # IOC must be terminal before the position snapshot can be trusted.
            try:
                order_status = await self.om.get_order_status(order_id)
            except Exception as exc:
                logger.error(f"Cannot confirm liquidation order {order_id}: {exc}")
                order_status = None
            positions = await self._verified_ticker_positions(target_ticker)
            if positions is None or not isinstance(order_status, dict) or order_status.get("status") not in ("executed", "canceled", "cancelled"):
                return False
            if any(positions.get(index, Decimal("0")) != previous
                   for index, previous in before_positions.items() if index != shard) or any(
                       value != 0 for index, value in positions.items()
                       if index != shard and index not in before_positions):
                return False
            new_inv = positions.get(shard, Decimal("0"))
            try:
                raw_fill = order_status.get("fill_count_fp", order_status.get("fill_count"))
                if raw_fill is None or isinstance(raw_fill, bool):
                    return False
                filled = Decimal(str(raw_fill))
                if not filled.is_finite() or filled < 0 or filled > slice_count:
                    return False
                expected = inv - filled if inv > 0 else inv + filled
                if new_inv != expected:
                    logger.error(f"Order and position snapshots disagree for {target_ticker}; refusing to continue.")
                    return False
            except (InvalidOperation, ValueError, TypeError):
                return False

            if (inv > 0 and new_inv < 0) or (inv < 0 and new_inv > 0):
                logger.error(
                    f"Liquidation position flipped from {inv} to {new_inv} on {target_ticker}; "
                    f"aborting to prevent oscillation."
                )
                return False

            if new_inv == 0 and not any(value != 0 for value in positions.values()):
                if (self.om._pending_client_ids or any(order.get("ticker") == target_ticker
                        for order in self.om.active_orders.values())
                        or self.inv_manager.get_position(target_ticker) != 0):
                    logger.error(f"Liquidation of {target_ticker} awaits pending orders or local fill reconciliation.")
                    return False
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

        logger.error(f"Liquidation incomplete for {target_ticker} after {total_slices} slices.")
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

        quoted_ids = {order_id for _, order_id in cancel_targets}
        for order_id, details in list(self.om.active_orders.items()):
            if details.get("ticker") == target_ticker and order_id not in quoted_ids:
                tasks.append(self.om.cancel_order(order_id))
                cancel_targets.append(("tracked", order_id))
             
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
                elif side == "ask":
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

        if self.om._pending_client_ids:
            logger.warning(f"Pending order placements prevent confirming withdrawal for {target_ticker}.")
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

        # Confirm all shards, including when the local feed currently appears flat.
        positions = await self._verified_ticker_positions(old_ticker)
        if positions is None:
            return False
        if any(value != 0 for value in positions.values()) or self.inv_manager.get_position(old_ticker) != 0:
            if not allow_liquidation:
                return False
            liquidation_ok = await self.liquidate_inventory(ticker=old_ticker)
            positions = await self._verified_ticker_positions(old_ticker)
            if not liquidation_ok or positions is None or any(value != 0 for value in positions.values()):
                await send_alert(f"⚠️ Rotation aborted: liquidation of {old_ticker} incomplete or unconfirmed.")
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

        # 1c. Sweep excess collateral from old trading shard back to Shard 0 if applicable
        old_exchange_index = getattr(self, "exchange_index", None)
        new_exchange_index = await asyncio.to_thread(get_market_exchange_index, new_ticker)
        # Delayed WebSocket fills must not be ignored between the REST check and swap.
        positions = await self._verified_ticker_positions(old_ticker)
        if positions is None or any(value != 0 for value in positions.values()) or self.inv_manager.get_position(old_ticker) != 0:
            logger.error(f"Aborting rotation: {old_ticker} position is nonzero or unconfirmed.")
            return False

        # 2. Swap orderbook subscription
        verified_fill_count = self.inv_manager._fill_count
        await self.ob_manager.unsubscribe([old_ticker])
        await self.ob_manager.subscribe([new_ticker])
        positions = await self._verified_ticker_positions(old_ticker)
        if (positions is None or any(value != 0 for value in positions.values())
                or self.inv_manager._fill_count != verified_fill_count
                or self.inv_manager.get_position(old_ticker) != 0):
            await self.ob_manager.unsubscribe([new_ticker])
            await self.ob_manager.subscribe([old_ticker])
            logger.error(f"Aborting rotation: fill or unconfirmed position during subscription swap for {old_ticker}.")
            return False

        if (self.auto_sweep_reverse_enabled and old_exchange_index not in (0, None)
                and new_exchange_index != old_exchange_index):
            await self.sweep_shard_to_shard0(source_shard=old_exchange_index)
            positions = await self._verified_ticker_positions(old_ticker)
            if (positions is None or any(value != 0 for value in positions.values())
                    or self.inv_manager._fill_count != verified_fill_count
                    or self.inv_manager.get_position(old_ticker) != 0):
                await self.ob_manager.unsubscribe([new_ticker])
                await self.ob_manager.subscribe([old_ticker])
                logger.error(f"Aborting rotation: fill or unconfirmed position during collateral sweep for {old_ticker}.")
                return False

        # 3. Update target ticker and reset state trackers
        self.ticker = new_ticker
        self.exchange_index = new_exchange_index
        self._consecutive_hedge_failures = 0
        self._starvation_start_time = None
        self._starvation_alert_sent = False
        self._last_empty_ob_log = 0.0
        self._last_market_status_check = time.time()
        self._market_inactive = False
        self._in_expiration_blackout = False
        self._market_close_time = None
        self._last_pnl_snapshot = time.time()
        self._mid_price_history.clear()
        if hasattr(self, "volatility_estimator"):
            self.volatility_estimator.reset()
        self._fast_market_until = 0.0
        self._market_attached_time = time.time()
        self._last_slate_check = time.time()

        # Start a new tracking session for the new market ticker
        self.inv_manager.pnl_tracker.reset_market_session(new_ticker)

        # 4. Ensure collateral on new shard and refresh metadata before quoting commences
        await self.ensure_shard_collateral()
        await self._refresh_market_metadata()

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
                    exclude_tickers=self._get_rotation_exclude_tickers(exclude_event=False),
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
        if not self._is_inventory_flat():
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
        self._stopping = True
        self.running = False
        clean = False
        generation = self._fill_connection_generation

        def feed_healthy():
            return (generation is not None and not self._ingestion_failed and self.ws_client.is_connected
                    and self.ws_client.connection_generation == generation
                    and self._ws_task is not None and not self._ws_task.done()
                    and self.inv_manager._fill_failure is None
                    and self.ws_client.__dict__.get("_ingestion_failure") is None)

        try:
            clean = await self._stop_before_ingestion_close(feed_healthy)
            return clean
        finally:
            if not clean:
                self._ingestion_failed = True
            tasks = [task for task in self._background_tasks if not task.done()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._ws_task is not None:
                if not self._ws_task.done():
                    self._ws_task.cancel()
                await asyncio.gather(self._ws_task, return_exceptions=True)
            ingestion_drained = False
            try:
                ingestion_drained = await self.inv_manager.close_fill_ingestion(clean=clean)
            except Exception as e:
                logger.error(f"Error closing fill ingestion: {e}")
            finally:
                if (
                    ingestion_drained
                    or getattr(self.inv_manager, "_fill_worker", None) is None
                    or self.inv_manager._fill_worker.done()
                ):
                    self.om.close()
                else:
                    logger.warning("Deferring OrderManager database pool closure because fill worker is still active.")

    async def _stop_before_ingestion_close(self, feed_healthy) -> bool:
        self.running = False

        if self._strategy_task is not None and not self._strategy_task.done():
            self._strategy_task.cancel()
            await asyncio.gather(self._strategy_task, return_exceptions=True)

        if self._ws_task is not None and self._ws_task.done() and not self._ws_task.cancelled():
            self._ingestion_failed = True

        if not feed_healthy():
            self._ingestion_failed = True

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

        # 1b. Confirm exchange inventory even if the local feed appears flat.
        verified_fill_count = None
        if quotes_cancelled and not self._ingestion_failed:
            positions = await self._verified_ticker_positions(self.ticker)
            if positions is None:
                logger.error(f"Failed to verify exchange inventory on {self.ticker} during shutdown.")
                quotes_cancelled = False
            elif any(value != 0 for value in positions.values()) or not self._is_inventory_flat():
                logger.warning(f"Liquidating remaining inventory on shutdown for {self.ticker}...")
                try:
                    liquidation_ok = await self.liquidate_inventory(self.ticker)
                except Exception as e:
                    logger.error(f"Shutdown liquidation raised for {self.ticker}: {e}")
                    liquidation_ok = False
                positions = await self._verified_ticker_positions(self.ticker)
                if (not liquidation_ok or positions is None
                        or any(value != 0 for value in positions.values())
                        or not self._is_inventory_flat()):
                    logger.error(f"Failed to confirm flat inventory on {self.ticker} during shutdown.")
                    quotes_cancelled = False
            if quotes_cancelled:
                verified_fill_count = self.inv_manager._fill_count
        elif not self._is_inventory_flat():
            if self._ingestion_failed or self._ws_task is not None or self._fill_connection_generation is not None:
                logger.error("Skipping unverified local inventory liquidation on shutdown: fill ingestion has failed or was active.")
                quotes_cancelled = False
            else:
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

        if quotes_cancelled and not self._ingestion_failed:
            positions = await self._verified_ticker_positions(self.ticker)
            if (positions is None or any(value != 0 for value in positions.values())
                    or not self._is_inventory_flat()
                    or self.inv_manager._fill_count != verified_fill_count):
                logger.error(f"Shutdown position changed or is unconfirmed for {self.ticker}.")
                quotes_cancelled = False

        # 2b. Sweep remaining funds back to Shard 0 on shutdown
        if not feed_healthy():
            self._ingestion_failed = True
        if self.auto_sweep_reverse_enabled and quotes_cancelled and not self._ingestion_failed:
            try:
                await self.sweep_shard_to_shard0(retain_dollars=0.0)
            except Exception as e:
                logger.error(f"Error during shutdown reverse sweep to Shard 0: {e}")

        # 3. Cancel and await all active background tasks
        tasks_to_cancel = [t for t in list(self._background_tasks) if not t.done()]
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        if not feed_healthy():
            self._ingestion_failed = True
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            await asyncio.gather(self._ws_task, return_exceptions=True)
            if not self._ws_task.cancelled():
                self._ingestion_failed = True

        if quotes_cancelled and not self._ingestion_failed:
            positions = await self._verified_ticker_positions(self.ticker)
            if (positions is None or any(value != 0 for value in positions.values())
                    or not self._is_inventory_flat()
                    or self.inv_manager._fill_count != verified_fill_count):
                logger.error(f"Shutdown position changed or is unconfirmed for {self.ticker}.")
                quotes_cancelled = False

        return (quotes_cancelled and not self._ingestion_failed
                and self.inv_manager._fill_failure is None
                and self.ws_client.__dict__.get("_ingestion_failure") is None)


# Backward-compatible alias
MarketMaker = AvellanedaStoikovBot
