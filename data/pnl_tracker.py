"""
Real-Time PnL Tracker and Trade Attribution Engine

This module implements FIFO (First-In, First-Out) lot matching for executed contracts,
distinguishing between Realized PnL (locked in upon position closing) and
Unrealized PnL (mark-to-market against live orderbook mid-price), while tracking
exchange fees and session attribution.
"""

import time
import uuid
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("PnLTracker")


@dataclass
class InventoryLot:
    """Represents an open contract lot awaiting offset."""
    lot_id: str
    ticker: str
    action: str  # "buy" (long) or "sell" (short)
    price_cents: Optional[float]  # None if cost basis is uncosted/unknown
    count: int
    timestamp: float
    is_uncosted: bool = False
    entry_fee_per_contract: float = 0.0


@dataclass
class MarketPnL:
    """Maintains PnL and open lots for a single market ticker."""
    ticker: str
    open_lots: List[InventoryLot] = field(default_factory=list)
    realized_pnl_cents: float = 0.0
    unrealized_pnl_cents: float = 0.0
    total_fees_cents: float = 0.0
    round_trips_count: int = 0
    winning_trades_count: int = 0
    losing_trades_count: int = 0
    scratch_trades_count: int = 0
    total_volume_contracts: int = 0
    last_mid_price: Optional[float] = None
    rotation_session_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class PnLTracker:
    """
    Tracks real-time Realized and Unrealized PnL across markets using FIFO lot matching.
    """

    def __init__(self):
        self.markets: Dict[str, MarketPnL] = {}

    def get_or_create_market(self, ticker: str) -> MarketPnL:
        """Retrieves or initializes PnL state for a market ticker."""
        if ticker not in self.markets:
            self.markets[ticker] = MarketPnL(ticker=ticker)
        return self.markets[ticker]

    def reset_market_session(self, ticker: str, new_session_id: Optional[str] = None) -> str:
        """
        Resets session-level attribution for a market upon rotation.
        Generates a new rotation session UUID while preserving cumulative market totals.
        """
        market = self.get_or_create_market(ticker)
        market.rotation_session_id = new_session_id or str(uuid.uuid4())
        return market.rotation_session_id

    def seed_initial_inventory(self, ticker: str, position: int, cost_basis_cents: Optional[float] = None):
        """
        Seeds open inventory lots when hydrating an existing position on startup.
        Prevents position distortion if the bot restarts with open contracts.
        Never fabricates an arbitrary price if true cost basis cannot be determined.
        """
        if position == 0:
            return
        market = self.get_or_create_market(ticker)
        if not market.open_lots:
            action = "buy" if position > 0 else "sell"
            if cost_basis_cents is not None:
                price = float(cost_basis_cents)
                is_uncosted = False
            else:
                price = None
                is_uncosted = True

            lot = InventoryLot(
                lot_id=f"init-{str(uuid.uuid4())[:6]}",
                ticker=ticker,
                action=action,
                price_cents=price,
                count=abs(position),
                timestamp=time.time(),
                is_uncosted=is_uncosted,
            )
            market.open_lots.append(lot)
            if is_uncosted:
                logger.warning(
                    f"Seeded uncosted initial lot for {ticker}: {abs(position)} {action.upper()}. "
                    f"Cost basis could not be determined; closing trades will not fabricate realized PnL."
                )
            else:
                logger.info(f"Seeded initial lot for {ticker}: {abs(position)} {action.upper()} @ {price:.1f}c.")

    @staticmethod
    def _calculate_lot_mtm(lot: InventoryLot, count: int, mid_price: Optional[float]) -> float:
        """
        Calculates the net mark-to-market PnL contribution for `count` contracts of `lot`.
        Deducts allocated entry fees so net MTM equals: gross_mtm - allocated_entry_fee.
        If the lot is uncosted, or mid_price is unknown, only the entry fee is known.
        """
        allocated_fee = lot.entry_fee_per_contract * count
        if lot.is_uncosted or lot.price_cents is None or mid_price is None:
            return -allocated_fee
        if lot.action == "buy":
            gross = (mid_price - lot.price_cents) * count
        elif lot.action == "sell":
            gross = (lot.price_cents - mid_price) * count
        else:
            gross = 0.0
        return gross - allocated_fee

    def reconcile_inventory(self, ticker: str, target_position: int):
        """
        Reconciles the tracker's lot-based inventory against authoritative REST portfolio positions.
        - Trims or flattens lots if authoritative position is smaller.
        - Realizes net mark-to-market PnL (including deferred entry fees) on removed costed lots so Total Strategy PnL is conserved.
        - Appends an uncosted lot if position has expanded or flipped across zero.
        """
        market = self.get_or_create_market(ticker)
        current_position = self.get_open_inventory(ticker)
        delta = target_position - current_position

        if delta == 0:
            return

        logger.warning(
            f"Reconciling inventory drift for {ticker}: tracker has {current_position}, "
            f"authoritative REST has {target_position} (delta={delta:+d}). Adjusting lots."
        )

        if target_position == 0:
            reconciled_mtm = sum(
                self._calculate_lot_mtm(lot, lot.count, market.last_mid_price)
                for lot in market.open_lots
            )
            if reconciled_mtm != 0.0:
                market.realized_pnl_cents += reconciled_mtm
                logger.warning(
                    f"Reconciled position to 0 for {ticker}: realized {reconciled_mtm:+.2f}c in "
                    f"net mark-to-market PnL on {len(market.open_lots)} closed lots."
                )
            market.open_lots.clear()
        elif current_position == 0:
            action = "buy" if target_position > 0 else "sell"
            market.open_lots.append(
                InventoryLot(
                    lot_id=f"recon-{str(uuid.uuid4())[:6]}",
                    ticker=ticker,
                    action=action,
                    price_cents=None,
                    count=abs(target_position),
                    timestamp=time.time(),
                    is_uncosted=True,
                    entry_fee_per_contract=0.0,
                )
            )
        elif (current_position > 0 and target_position > 0) or (current_position < 0 and target_position < 0):
            if abs(target_position) > abs(current_position):
                action = "buy" if target_position > 0 else "sell"
                market.open_lots.append(
                    InventoryLot(
                        lot_id=f"recon-{str(uuid.uuid4())[:6]}",
                        ticker=ticker,
                        action=action,
                        price_cents=None,
                        count=abs(delta),
                        timestamp=time.time(),
                        is_uncosted=True,
                        entry_fee_per_contract=0.0,
                    )
                )
            else:
                trim_needed = abs(delta)
                new_lots = []
                reconciled_mtm = 0.0
                for lot in market.open_lots:
                    if trim_needed > 0:
                        trimmed_count = min(lot.count, trim_needed)
                        reconciled_mtm += self._calculate_lot_mtm(lot, trimmed_count, market.last_mid_price)
                        trim_needed -= trimmed_count
                        remaining = lot.count - trimmed_count
                        if remaining > 0:
                            lot.count = remaining
                            new_lots.append(lot)
                    else:
                        new_lots.append(lot)
                market.open_lots = new_lots
                if reconciled_mtm != 0.0:
                    market.realized_pnl_cents += reconciled_mtm
                    logger.warning(
                        f"Trimmed {abs(delta)} contracts during reconciliation for {ticker}: "
                        f"realized {reconciled_mtm:+.2f}c in net mark-to-market PnL."
                    )
        else:
            reconciled_mtm = sum(
                self._calculate_lot_mtm(lot, lot.count, market.last_mid_price)
                for lot in market.open_lots
            )
            if reconciled_mtm != 0.0:
                market.realized_pnl_cents += reconciled_mtm
                logger.warning(
                    f"Reconciled position reversal for {ticker}: realized {reconciled_mtm:+.2f}c in "
                    f"net mark-to-market PnL on {len(market.open_lots)} closed lots."
                )
            market.open_lots = [
                InventoryLot(
                    lot_id=f"recon-{str(uuid.uuid4())[:6]}",
                    ticker=ticker,
                    action="buy" if target_position > 0 else "sell",
                    price_cents=None,
                    count=abs(target_position),
                    timestamp=time.time(),
                    is_uncosted=True,
                    entry_fee_per_contract=0.0,
                )
            ]

        if market.last_mid_price is not None:
            self.update_mid_price(ticker, market.last_mid_price)


    @staticmethod
    def normalize_fill(action: str, side: str, price_cents: float) -> Tuple[str, float]:
        """
        Normalizes contract fills to YES-equivalent direction and pricing.
        - Buy YES at P -> Buy YES at P
        - Sell YES at P -> Sell YES at P
        - Buy NO at P  -> Sell YES at (100 - P)
        - Sell NO at P -> Buy YES at (100 - P)
        """
        norm_action = action.lower()
        norm_side = side.lower()
        price = float(price_cents)

        if norm_side == "no":
            norm_action = "sell" if norm_action == "buy" else "buy"
            norm_price = 100.0 - price
        else:
            norm_price = price

        return norm_action, norm_price

    def record_fill(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_cents: float,
        fee_cents: float = 0.0,
        timestamp: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Processes a fill through the FIFO queue, matching offsetting inventory
        and computing realized gain/loss.

        Returns a dictionary summarizing the fill execution impact.
        """
        if count <= 0:
            return {}

        now = timestamp or time.time()
        market = self.get_or_create_market(ticker)
        market.total_volume_contracts += count
        market.total_fees_cents += float(fee_cents)

        norm_action, norm_price = self.normalize_fill(action, side, price_cents)
        remaining_count = count
        realized_delta = 0.0

        # Opposing action that would be closed by this fill:
        # If this fill is 'sell', it closes open 'buy' lots.
        # If this fill is 'buy', it closes open 'sell' lots.
        target_close_action = "buy" if norm_action == "sell" else "sell"

        # FIFO matching against open lots
        new_open_lots: List[InventoryLot] = []
        matched_lots_info: List[Tuple[int, float, bool, float]] = []

        for lot in market.open_lots:
            if remaining_count > 0 and lot.action == target_close_action:
                matched_count = min(remaining_count, lot.count)

                if lot.is_uncosted or lot.price_cents is None:
                    # Cost basis unknown; cannot calculate real PnL without fabricating numbers
                    trade_pnl = 0.0
                    is_uncosted = True
                    logger.warning(
                        f"Closing {matched_count} uncosted contracts from seeded lot {lot.lot_id} on {ticker}; "
                        f"cost basis was indeterminate, so no realized PnL is fabricated."
                    )
                elif lot.action == "buy":
                    # We were long at lot.price_cents, now selling at norm_price
                    trade_pnl = (norm_price - lot.price_cents) * matched_count
                    is_uncosted = False
                else:
                    # We were short at lot.price_cents, now buying back at norm_price
                    trade_pnl = (lot.price_cents - norm_price) * matched_count
                    is_uncosted = False

                matched_lots_info.append((matched_count, trade_pnl, is_uncosted, lot.entry_fee_per_contract))

                lot.count -= matched_count
                remaining_count -= matched_count

                if lot.count > 0:
                    new_open_lots.append(lot)
            else:
                new_open_lots.append(lot)

        market.open_lots = new_open_lots

        # Classify outcomes net of transaction fees (accounting for both entry and closing fees)
        total_matched_contracts = count - remaining_count
        matched_outcomes: List[str] = []
        for matched_count, gross_pnl, is_uncosted, entry_fee_per_contract in matched_lots_info:
            closing_lot_fee = (float(fee_cents) * matched_count / count) if count > 0 else 0.0
            if is_uncosted:
                # Deduct closing fees actually incurred, but do not fabricate gross trading PnL or win/loss classification
                realized_delta -= closing_lot_fee
                continue

            entry_lot_fee = entry_fee_per_contract * matched_count
            net_trade_pnl = gross_pnl - (closing_lot_fee + entry_lot_fee)
            realized_delta += net_trade_pnl

            market.round_trips_count += 1
            if net_trade_pnl > 0.0001:
                market.winning_trades_count += 1
                matched_outcomes.append("profit")
            elif net_trade_pnl < -0.0001:
                market.losing_trades_count += 1
                matched_outcomes.append("loss")
            else:
                market.scratch_trades_count += 1
                matched_outcomes.append("scratch")

        market.realized_pnl_cents += realized_delta

        # If any contracts remain after closing existing lots, open a new lot
        if remaining_count > 0:
            entry_fee_per_contract = (float(fee_cents) / count) if count > 0 else 0.0
            new_lot = InventoryLot(
                lot_id=str(uuid.uuid4())[:8],
                ticker=ticker,
                action=norm_action,
                price_cents=norm_price,
                count=remaining_count,
                timestamp=now,
                is_uncosted=False,
                entry_fee_per_contract=entry_fee_per_contract,
            )
            market.open_lots.append(new_lot)

        # Recalculate unrealized PnL if mid-price is known
        if market.last_mid_price is not None:
            self.update_mid_price(ticker, market.last_mid_price)

        logger.info(
            f"PnL Fill [{ticker}]: {action.upper()} {count} {side.upper()} @ {price_cents}c | "
            f"Realized Delta: {realized_delta:+.2f}c | Cum Realized: {market.realized_pnl_cents:+.2f}c | "
            f"Open Lots: {len(market.open_lots)} ({sum(l.count for l in market.open_lots)} contracts)"
        )

        return {
            "ticker": ticker,
            "matched_contracts": total_matched_contracts,
            "matched_lots_count": len(matched_outcomes),
            "matched_outcomes": matched_outcomes,
            "new_contracts": remaining_count,
            "realized_delta_cents": realized_delta,
            "cumulative_realized_cents": market.realized_pnl_cents,
            "total_fees_cents": market.total_fees_cents,
            "open_lots_count": len(market.open_lots),
        }

    def update_mid_price(self, ticker: str, mid_price: float) -> float:
        """
        Updates mark-to-market unrealized PnL for open lots against current mid-price.
        Returns the updated unrealized PnL in cents.
        """
        market = self.get_or_create_market(ticker)
        market.last_mid_price = float(mid_price)

        unrealized = 0.0
        for lot in market.open_lots:
            if lot.is_uncosted or lot.price_cents is None:
                continue
            lot_entry_fee = lot.entry_fee_per_contract * lot.count
            if lot.action == "buy":
                # Long position marked to mid net of allocated entry fee
                unrealized += (mid_price - lot.price_cents) * lot.count - lot_entry_fee
            elif lot.action == "sell":
                # Short position marked to mid net of allocated entry fee
                unrealized += (lot.price_cents - mid_price) * lot.count - lot_entry_fee

        market.unrealized_pnl_cents = unrealized
        return unrealized

    def get_realized_pnl(self, ticker: str) -> float:
        """Returns cumulative realized PnL in cents for a market."""
        return self.get_or_create_market(ticker).realized_pnl_cents

    def get_unrealized_pnl(self, ticker: str) -> float:
        """Returns current unrealized mark-to-market PnL in cents for a market."""
        return self.get_or_create_market(ticker).unrealized_pnl_cents

    def get_total_fees(self, ticker: str) -> float:
        """Returns cumulative fees paid in cents for a market."""
        return self.get_or_create_market(ticker).total_fees_cents

    def get_open_inventory(self, ticker: str) -> int:
        """
        Returns net open contract inventory.
        Positive = net long YES, Negative = net short YES.
        """
        market = self.get_or_create_market(ticker)
        net_pos = 0
        for lot in market.open_lots:
            if lot.action == "buy":
                net_pos += lot.count
            elif lot.action == "sell":
                net_pos -= lot.count
        return net_pos

    def get_market_summary(self, ticker: str) -> Dict[str, Any]:
        """Returns a complete performance summary for a market."""
        market = self.get_or_create_market(ticker)
        realized = round(market.realized_pnl_cents, 4)
        unrealized = round(market.unrealized_pnl_cents, 4)
        total_pnl = round(realized + unrealized, 4)
        total_fees = round(market.total_fees_cents, 4)
        net_inventory = self.get_open_inventory(ticker)

        return {
            "ticker": ticker,
            "realized_pnl_cents": realized,
            "unrealized_pnl_cents": unrealized,
            "total_pnl_cents": total_pnl,
            "total_fees_cents": total_fees,
            "net_inventory": net_inventory,
            "open_lots_count": len(market.open_lots),
            "round_trips_count": market.round_trips_count,
            "winning_trades": market.winning_trades_count,
            "losing_trades": market.losing_trades_count,
            "scratch_trades": market.scratch_trades_count,
            "volume_contracts": market.total_volume_contracts,
            "rotation_session_id": market.rotation_session_id,
            "last_mid_price": market.last_mid_price,
        }
