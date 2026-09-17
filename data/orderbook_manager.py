"""
Orderbook Manager

This module tracks and maintains the real-time L2 order book (bids and asks) 
for the selected prediction markets. It processes full orderbook snapshots 
and incremental delta updates received from the Kalshi WebSocket feeds.
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("OrderbookManager")

def _normalize_price_to_cents(price: Any) -> Optional[int]:
    if price is None:
        return None
    try:
        p_str = str(price).strip()
        p_val = float(p_str)
        if p_val < 1.0:
            return round(p_val * 100)
        return int(round(p_val))
    except (ValueError, TypeError):
        return None


def _normalize_qty(qty: Any) -> int:
    if qty is None:
        return 0
    try:
        return int(round(float(qty)))
    except (ValueError, TypeError):
        return 0


class OrderbookManager:
    """
    Maintains a real-time L2 order book for tracked Kalshi markets.
    Supports both Kalshi v2 dollar-formatted feeds (yes_dollars_fp, price_dollars)
    and legacy integer cent feeds.
    """
    def __init__(self, ws_client):
        self.ws_client = ws_client
        # Format: self.books[ticker][side][price] = quantity
        # side: "yes" or "no"
        self.books: Dict[str, Dict[str, Dict[int, int]]] = {}
        
        # Register the message handler with the websocket client
        self.ws_client.add_message_handler(self._handle_message)

    async def subscribe(self, tickers: list[str]):
        """Subscribe to orderbook deltas for the given tickers."""
        # Initialize empty books for new tickers
        for ticker in tickers:
            if ticker not in self.books:
                self.books[ticker] = {"yes": {}, "no": {}}
        
        await self.ws_client.subscribe(["orderbook_delta"], tickers)

    async def unsubscribe(self, tickers: list[str]):
        """Unsubscribe from orderbook deltas for the given tickers and clean up cached books."""
        for ticker in tickers:
            self.books.pop(ticker, None)
        await self.ws_client.unsubscribe(["orderbook_delta"], tickers)


    async def _handle_message(self, message: Dict[str, Any]):
        msg_type = message.get("type")
        
        if msg_type == "orderbook_snapshot":
            self._handle_snapshot(message.get("msg", {}))
        elif msg_type == "orderbook_delta":
            self._handle_delta(message.get("msg", {}))

    def _handle_snapshot(self, msg: Dict[str, Any]):
        ticker = msg.get("market_ticker")
        if not ticker:
            return
            
        if ticker not in self.books:
            self.books[ticker] = {"yes": {}, "no": {}}
            
        logger.info(f"Received orderbook snapshot for {ticker}")
        
        # Load snapshot bids/asks ("yes" and "no", or v2 "yes_dollars_fp" and "no_dollars_fp")
        for side in ["yes", "no"]:
            levels = (
                msg.get(f"{side}_dollars_fp") or
                msg.get(f"{side}_dollars") or
                msg.get(side, [])
            )
            side_book: Dict[int, int] = {}
            for item in levels:
                if len(item) >= 2:
                    price_cents = _normalize_price_to_cents(item[0])
                    qty = _normalize_qty(item[1])
                    if price_cents is not None and qty > 0 and 1 <= price_cents <= 99:
                        side_book[price_cents] = qty
            self.books[ticker][side] = side_book

    def _handle_delta(self, msg: Dict[str, Any]):
        ticker = msg.get("market_ticker")
        if not ticker or ticker not in self.books:
            return
            
        side = msg.get("side") # "yes" or "no"
        raw_price = msg.get("price_dollars") if msg.get("price_dollars") is not None else msg.get("price")
        raw_delta = msg.get("delta_fp") if msg.get("delta_fp") is not None else msg.get("delta")
        
        if side not in self.books[ticker] or raw_price is None or raw_delta is None:
            return

        price_cents = _normalize_price_to_cents(raw_price)
        if price_cents is None:
            return

        try:
            delta = float(raw_delta)
        except (ValueError, TypeError):
            return
            
        current_qty = self.books[ticker][side].get(price_cents, 0)
        new_qty = current_qty + delta
        
        if new_qty <= 0:
            self.books[ticker][side].pop(price_cents, None)
        else:
            self.books[ticker][side][price_cents] = int(round(new_qty))

    def get_best_bid(self, ticker: str) -> Optional[tuple[int, int]]:
        """
        Returns the best bid for 'yes' shares as (price, quantity).
        The best bid is the highest price someone is willing to buy 'yes' shares at.
        On Kalshi, you buy 'yes' from the 'yes' side order book. 
        Actually, buying 'yes' means someone posted a bid.
        Wait, Kalshi's orderbook reports 'yes' and 'no' sides.
        The 'yes' array represents resting limit orders asking to BUY 'yes' shares (bids),
        or are they asks to SELL 'yes' shares?
        Usually, 'yes' array in Kalshi snapshot represents resting YES bids.
        The 'no' array represents resting NO bids (which are equivalent to YES asks conceptually).
        """
        if ticker not in self.books or not self.books[ticker]["yes"]:
            return None
        # Highest price on 'yes' side
        best_price = max(self.books[ticker]["yes"].keys())
        return best_price, self.books[ticker]["yes"][best_price]

    def get_best_ask(self, ticker: str) -> Optional[tuple[int, int]]:
        """
        Returns the best ask for 'yes' shares as (price, quantity).
        On Kalshi, buying 'no' at price P is equivalent to selling 'yes' at 100 - P.
        So resting 'no' bids represent 'yes' asks.
        The best ask is the lowest price someone is willing to sell 'yes' at.
        Resting 'no' bids at price P means they want to buy NO for P.
        Therefore, you can buy YES from them for (100 - P).
        So to find the best ask for YES, we find the highest NO bid price P, 
        and the YES ask price is 100 - P.
        """
        if ticker not in self.books or not self.books[ticker]["no"]:
            return None
            
        # Highest price on 'no' side
        highest_no_bid = max(self.books[ticker]["no"].keys())
        # Implied yes ask price = 100 - highest_no_bid (assuming 1-99 range cents)
        implied_ask_price = 100 - highest_no_bid
        
        return implied_ask_price, self.books[ticker]["no"][highest_no_bid]
