#!/usr/bin/env python3
"""
Automated Daily Trading Performance Analysis & Reporting Tool.

Queries PostgreSQL for order lifecycle and PnL attribution snapshots
over a 24-hour trading session (in local/EDT or UTC time) and generates
a structured, quantitative post-session analysis report in GitHub-flavored Markdown.
All monetary performance metrics and trading outcomes are reported in USD ($).
"""

import argparse
import datetime
import logging
import os
import sys
import zoneinfo
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

# Add repository root to path for config imports
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    import config
    DEFAULT_DB_HOST = getattr(config, "DB_HOST", "localhost")
    DEFAULT_DB_PORT = getattr(config, "DB_PORT", 5432)
    DEFAULT_DB_NAME = getattr(config, "DB_NAME", "kalshi_bot")
    DEFAULT_DB_USER = getattr(config, "DB_USER", "postgres")
    DEFAULT_DB_PASSWORD = getattr(config, "DB_PASSWORD", "postgres")
except ImportError:
    DEFAULT_DB_HOST = os.getenv("DB_HOST", "localhost")
    DEFAULT_DB_PORT = int(os.getenv("DB_PORT", "5432"))
    DEFAULT_DB_NAME = os.getenv("DB_NAME", "kalshi_bot")
    DEFAULT_DB_USER = os.getenv("DB_USER", "postgres")
    DEFAULT_DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

logger = logging.getLogger("DailyReportGenerator")


def get_db_connection(
    host: str = DEFAULT_DB_HOST,
    port: int = DEFAULT_DB_PORT,
    dbname: str = DEFAULT_DB_NAME,
    user: str = DEFAULT_DB_USER,
    password: str = DEFAULT_DB_PASSWORD,
):
    """Establishes a connection to the PostgreSQL database."""
    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        password=password,
        connect_timeout=10,
    )


def compute_fifo_round_trips(fills: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Reconstructs FIFO round-trip trades from executed fill records.
    Normalizes YES and NO side contracts and computes win rate, profit factor,
    gross wins/losses, and average trade metrics.
    """
    lots: List[Tuple[float, float, int]] = []  # (entry_price_cents, count, direction: 1 for long YES, -1 for short YES)
    round_trips: List[Dict[str, Any]] = []

    for f in fills:
        act = str(f.get("action") or "").lower()
        side_val = str(f.get("side") or "").lower()
        p = float(f.get("price") or 0)
        cnt = float(f.get("count") or 0)
        if cnt <= 0:
            continue

        # Direction convention: +1 = Long YES, -1 = Short YES
        if side_val == "no":
            norm_side = -1 if act == "buy" else 1
            norm_price = 100.0 - p
        else:
            norm_side = 1 if act == "buy" else -1
            norm_price = p

        rem = cnt
        while lots and rem > 0:
            lot_p, lot_cnt, lot_side = lots[0]
            if lot_side != norm_side:
                match_cnt = min(rem, lot_cnt)
                gross_pnl = (norm_price - lot_p) * match_cnt if lot_side == 1 else (lot_p - norm_price) * match_cnt
                round_trips.append({
                    "entry_price": lot_p,
                    "exit_price": norm_price,
                    "count": match_cnt,
                    "gross_pnl": gross_pnl,
                    "gross_pnl_cents": gross_pnl,
                    "direction": "long" if lot_side == 1 else "short",
                    "timestamp": f.get("created_at"),
                })
                rem -= match_cnt
                if match_cnt == lot_cnt:
                    lots.pop(0)
                else:
                    lots[0] = (lot_p, lot_cnt - match_cnt, lot_side)
            else:
                break

        if rem > 0:
            lots.append((norm_price, rem, norm_side))

    total_rt = len(round_trips)
    wins = [rt for rt in round_trips if rt["gross_pnl"] > 0]
    losses = [rt for rt in round_trips if rt["gross_pnl"] < 0]
    scratches = [rt for rt in round_trips if rt["gross_pnl"] == 0]

    win_count = len(wins)
    loss_count = len(losses)
    scratch_count = len(scratches)

    win_rate = (win_count / total_rt * 100.0) if total_rt > 0 else 0.0
    win_rate_ex = (win_count / (win_count + loss_count) * 100.0) if (win_count + loss_count) > 0 else 0.0

    gross_wins = sum(rt["gross_pnl"] for rt in wins)
    gross_losses = sum(rt["gross_pnl"] for rt in losses)
    avg_win = (gross_wins / win_count) if win_count > 0 else 0.0
    avg_loss = (gross_losses / loss_count) if loss_count > 0 else 0.0
    profit_factor = abs(gross_wins / gross_losses) if gross_losses != 0 else (float("inf") if gross_wins > 0 else 0.0)

    return {
        "round_trips": round_trips,
        "total_round_trips": total_rt,
        "winning_trades": win_count,
        "losing_trades": loss_count,
        "scratch_trades": scratch_count,
        "win_rate_pct": win_rate,
        "win_rate_ex_scratches_pct": win_rate_ex,
        "gross_wins_cents": gross_wins,
        "gross_losses_cents": gross_losses,
        "net_gross_realized_cents": gross_wins + gross_losses,
        "avg_win_cents": avg_win,
        "avg_loss_cents": avg_loss,
        "profit_factor": profit_factor,
        "open_unmatched_contracts": sum(l[1] for l in lots),
    }


def fetch_daily_metrics(
    conn,
    target_date: datetime.date,
    tz_name: str = "America/New_York",
) -> Dict[str, Any]:
    """
    Extracts orders and PnL attribution snapshots for target_date in the specified timezone
    (00:00:00 to 23:59:59.999999 local time, converted to UTC for DB query).
    Computes performance attribution, inventory dynamics, fee drag, order velocity,
    and reconstructed FIFO round-trip trade statistics.
    """
    tz = zoneinfo.ZoneInfo(tz_name)
    start_local = datetime.datetime.combine(target_date, datetime.time.min, tzinfo=tz)
    end_local = datetime.datetime.combine(target_date, datetime.time.max, tzinfo=tz)

    start_utc = start_local.astimezone(datetime.timezone.utc)
    end_utc = end_local.astimezone(datetime.timezone.utc)

    metrics: Dict[str, Any] = {
        "date": target_date.isoformat(),
        "timezone": tz_name,
        "start_local": start_local.isoformat(),
        "end_local": end_local.isoformat(),
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "tickers": {},
        "totals": {
            "total_realized_delta_cents": 0.0,
            "total_unrealized_delta_cents": 0.0,
            "total_ending_unrealized_cents": 0.0,
            "total_fees_delta_cents": 0.0,
            "net_strategy_pnl_cents": 0.0,
            "total_orders": 0,
            "total_buy_orders": 0,
            "total_sell_orders": 0,
            "total_fills": 0,
            "total_cancels": 0,
            "total_contracts_quoted": 0,
            "active_market_count": 0,
            "snapshot_count": 0,
            "total_round_trips": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "scratch_trades": 0,
            "win_rate_pct": 0.0,
            "win_rate_ex_scratches_pct": 0.0,
            "gross_wins_cents": 0.0,
            "gross_losses_cents": 0.0,
            "net_gross_realized_cents": 0.0,
            "avg_win_cents": 0.0,
            "avg_loss_cents": 0.0,
            "profit_factor": 0.0,
        },
    }

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # 1. Fetch distinct active tickers in pnl_attribution
        cur.execute(
            """
            SELECT DISTINCT ticker 
            FROM pnl_attribution 
            WHERE timestamp >= %s AND timestamp <= %s
            ORDER BY ticker
            """,
            (start_utc, end_utc),
        )
        pnl_tickers = [row["ticker"] for row in cur.fetchall()]

        # 2. Fetch distinct active tickers in orders
        cur.execute(
            """
            SELECT DISTINCT ticker 
            FROM orders 
            WHERE created_at >= %s AND created_at <= %s
            ORDER BY ticker
            """,
            (start_utc, end_utc),
        )
        order_tickers = [row["ticker"] for row in cur.fetchall()]

        all_tickers = sorted(list(set(pnl_tickers + order_tickers)))
        metrics["totals"]["active_market_count"] = len(all_tickers)

        if not all_tickers:
            return metrics

        # 3. Aggregate per-ticker PnL attribution
        all_day_fills: List[Dict[str, Any]] = []

        for ticker in all_tickers:
            cur.execute(
                """
                SELECT 
                    COUNT(*) as snapshot_count,
                    MIN(inventory_at_snapshot) as min_inventory,
                    MAX(inventory_at_snapshot) as max_inventory,
                    AVG(inventory_at_snapshot) as avg_inventory,
                    COUNT(DISTINCT rotation_session_id) as session_count
                FROM pnl_attribution
                WHERE ticker = %s AND timestamp >= %s AND timestamp <= %s
                """,
                (ticker, start_utc, end_utc),
            )
            agg = cur.fetchone() or {}

            # Baseline snapshot: earliest within window
            cur.execute(
                """
                SELECT realized_pnl_cents, unrealized_pnl_cents, total_fees_cents, inventory_at_snapshot, timestamp
                FROM pnl_attribution
                WHERE ticker = %s AND timestamp >= %s AND timestamp <= %s
                ORDER BY timestamp ASC
                LIMIT 1
                """,
                (ticker, start_utc, end_utc),
            )
            first_snap = cur.fetchone()

            # Latest snapshot of the window
            cur.execute(
                """
                SELECT realized_pnl_cents, unrealized_pnl_cents, total_fees_cents, inventory_at_snapshot, timestamp
                FROM pnl_attribution
                WHERE ticker = %s AND timestamp >= %s AND timestamp <= %s
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (ticker, start_utc, end_utc),
            )
            last_snap = cur.fetchone()

            snap_count = agg.get("snapshot_count") or 0
            metrics["totals"]["snapshot_count"] += snap_count

            if first_snap and last_snap:
                realized_delta = float(last_snap["realized_pnl_cents"]) - float(first_snap["realized_pnl_cents"])
                fees_delta = float(last_snap["total_fees_cents"]) - float(first_snap["total_fees_cents"])
                unrealized_delta = float(last_snap["unrealized_pnl_cents"]) - float(first_snap["unrealized_pnl_cents"])
                ending_unrealized = float(last_snap["unrealized_pnl_cents"])
                ending_inventory = int(last_snap["inventory_at_snapshot"])
            else:
                realized_delta = 0.0
                fees_delta = 0.0
                unrealized_delta = 0.0
                ending_unrealized = 0.0
                ending_inventory = 0

            # 4. Aggregate orders per ticker
            cur.execute(
                """
                SELECT 
                    COUNT(*) as total_orders,
                    COUNT(*) FILTER (WHERE action = 'buy') as buy_orders,
                    COUNT(*) FILTER (WHERE action = 'sell') as sell_orders,
                    COUNT(*) FILTER (WHERE status = 'resting') as resting_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled_orders,
                    COUNT(*) FILTER (WHERE status = 'cancelled_or_filled_404') as executed_or_filled_404,
                    COALESCE(SUM(count), 0) as total_contracts_quoted,
                    AVG(price) as avg_price_cents
                FROM orders
                WHERE ticker = %s AND created_at >= %s AND created_at <= %s
                """,
                (ticker, start_utc, end_utc),
            )
            ord_agg = cur.fetchone() or {}

            total_orders = ord_agg.get("total_orders") or 0
            buy_orders = ord_agg.get("buy_orders") or 0
            sell_orders = ord_agg.get("sell_orders") or 0
            fills = ord_agg.get("executed_or_filled_404") or 0
            cancels = ord_agg.get("cancelled_orders") or 0
            total_contracts = ord_agg.get("total_contracts_quoted") or 0

            # 5. Fetch executed fill records for FIFO round-trip reconstruction
            cur.execute(
                """
                SELECT client_order_id, action, side, price, count, created_at
                FROM orders
                WHERE ticker = %s AND created_at >= %s AND created_at <= %s
                  AND status = 'cancelled_or_filled_404'
                ORDER BY created_at ASC
                """,
                (ticker, start_utc, end_utc),
            )
            ticker_fills = cur.fetchall() or []
            all_day_fills.extend(ticker_fills)
            ticker_rt = compute_fifo_round_trips(ticker_fills)

            ticker_metrics = {
                "snapshot_count": snap_count,
                "session_count": agg.get("session_count") or 0,
                "realized_delta_cents": realized_delta,
                "fees_delta_cents": fees_delta,
                "unrealized_delta_cents": unrealized_delta,
                "ending_unrealized_cents": ending_unrealized,
                "net_pnl_cents": realized_delta + unrealized_delta,
                "min_inventory": agg.get("min_inventory") if agg.get("min_inventory") is not None else 0,
                "max_inventory": agg.get("max_inventory") if agg.get("max_inventory") is not None else 0,
                "avg_inventory": float(agg.get("avg_inventory") or 0.0),
                "ending_inventory": ending_inventory,
                "total_orders": total_orders,
                "buy_orders": buy_orders,
                "sell_orders": sell_orders,
                "fills": fills,
                "cancelled_orders": cancels,
                "resting_orders": ord_agg.get("resting_orders") or 0,
                "total_contracts_quoted": total_contracts,
                "avg_price_cents": float(ord_agg.get("avg_price_cents") or 0.0),
                "trade_outcomes": ticker_rt,
            }

            metrics["tickers"][ticker] = ticker_metrics

            metrics["totals"]["total_realized_delta_cents"] += realized_delta
            metrics["totals"]["total_unrealized_delta_cents"] += unrealized_delta
            metrics["totals"]["total_fees_delta_cents"] += fees_delta
            metrics["totals"]["total_ending_unrealized_cents"] += ending_unrealized
            metrics["totals"]["net_strategy_pnl_cents"] += (realized_delta + unrealized_delta)
            metrics["totals"]["total_orders"] += total_orders
            metrics["totals"]["total_buy_orders"] += buy_orders
            metrics["totals"]["total_sell_orders"] += sell_orders
            metrics["totals"]["total_fills"] += fills
            metrics["totals"]["total_cancels"] += cancels
            metrics["totals"]["total_contracts_quoted"] += total_contracts

        # Overall FIFO round-trip trade totals aggregated per-ticker to avoid cross-ticker lot matching
        all_round_trips = []
        for t_data in metrics["tickers"].values():
            all_round_trips.extend(t_data.get("trade_outcomes", {}).get("round_trips", []))

        total_rts = len(all_round_trips)
        wins = sum(1 for rt in all_round_trips if rt.get("gross_pnl_cents", rt.get("gross_pnl", 0.0)) > 0)
        losses = sum(1 for rt in all_round_trips if rt.get("gross_pnl_cents", rt.get("gross_pnl", 0.0)) < 0)
        scratches = sum(1 for rt in all_round_trips if rt.get("gross_pnl_cents", rt.get("gross_pnl", 0.0)) == 0)
        gross_wins = sum(rt.get("gross_pnl_cents", rt.get("gross_pnl", 0.0)) for rt in all_round_trips if rt.get("gross_pnl_cents", rt.get("gross_pnl", 0.0)) > 0)
        gross_losses = sum(rt.get("gross_pnl_cents", rt.get("gross_pnl", 0.0)) for rt in all_round_trips if rt.get("gross_pnl_cents", rt.get("gross_pnl", 0.0)) < 0)
        net_gross = gross_wins + gross_losses

        metrics["totals"]["total_round_trips"] = total_rts
        metrics["totals"]["winning_trades"] = wins
        metrics["totals"]["losing_trades"] = losses
        metrics["totals"]["scratch_trades"] = scratches
        metrics["totals"]["win_rate_pct"] = (wins / total_rts * 100.0) if total_rts > 0 else 0.0
        decisive = wins + losses
        metrics["totals"]["win_rate_ex_scratches_pct"] = (wins / decisive * 100.0) if decisive > 0 else 0.0
        metrics["totals"]["gross_wins_cents"] = gross_wins
        metrics["totals"]["gross_losses_cents"] = gross_losses
        metrics["totals"]["net_gross_realized_cents"] = net_gross
        metrics["totals"]["avg_win_cents"] = (gross_wins / wins) if wins > 0 else 0.0
        metrics["totals"]["avg_loss_cents"] = (gross_losses / losses) if losses > 0 else 0.0
        metrics["totals"]["profit_factor"] = (gross_wins / abs(gross_losses)) if gross_losses != 0 else (999.0 if gross_wins > 0 else 0.0)

    return metrics


def generate_recommendations(metrics: Dict[str, Any]) -> List[str]:
    """Generates algorithmic diagnostic recommendations based on session statistics."""
    recommendations: List[str] = []
    totals = metrics.get("totals", {})

    realized_cents = totals.get("total_realized_delta_cents", 0.0)
    fees_cents = totals.get("total_fees_delta_cents", 0.0)
    orders_total = totals.get("total_orders", 0)
    fills_total = totals.get("total_fills", 0)
    snapshots_total = totals.get("snapshot_count", 0)

    realized_dlr = realized_cents / 100.0
    fees_dlr = fees_cents / 100.0

    # 1. Zero order quiesce check
    if orders_total == 0 and snapshots_total > 0:
        recommendations.append(
            "**No Order Placement Recorded**: The bot completed quoting snapshots but placed 0 orders. "
            "Verify market discovery criteria, market status (e.g. open vs closed), and collar boundaries."
        )

    # 2. Fee drag analysis
    if fees_cents > 0:
        if realized_cents > 0 and (fees_cents / realized_cents) > 0.40:
            recommendations.append(
                f"**High Fee Drag (${fees_dlr:.2f} fees vs ${realized_dlr:.2f} realized)**: "
                "Exchange fees consumed over 40% of gross realized profits. Consider widening `MIN_SPREAD` "
                "(e.g., from $0.02 to $0.03–$0.04) or slightly increasing `POST_FILL_PAUSE_SECONDS` to reduce rapid fee churn."
            )
        elif realized_cents < 0 and fees_cents > abs(realized_cents) * 0.5:
            recommendations.append(
                f"**Fee-Dominated Drawdown (${fees_dlr:.2f} fees)**: Fees represented a majority component "
                f"({(fees_cents / abs(realized_cents) * 100):.1f}%) of session drawdown. Review quoting frequency and "
                "avoid quoting tightly in contracts where maker fees exceed edge."
            )

    # 3. Inventory skew and liquidation drag
    extreme_skew_tickers = []
    liquidated_tickers = []
    for ticker, t_data in metrics.get("tickers", {}).items():
        min_inv = t_data.get("min_inventory", 0)
        max_inv = t_data.get("max_inventory", 0)
        t_real = t_data.get("realized_delta_cents", 0.0)
        if abs(min_inv) >= 10 or abs(max_inv) >= 10:
            extreme_skew_tickers.append((ticker, min_inv, max_inv))
        if t_real < -50.0 and (abs(min_inv) >= 4 or abs(max_inv) >= 4):
            liquidated_tickers.append((ticker, t_real / 100.0))

    if extreme_skew_tickers:
        details = ", ".join([f"`{t}` (range: [{low}, +{high}])" for t, low, high in extreme_skew_tickers])
        recommendations.append(
            f"**Substantial Inventory Skew Detected**: The following markets accumulated high one-sided exposure: {details}. "
            "Consider increasing `RISK_GAMMA` (e.g. from 0.7 to 0.8–1.0) to shade reservation prices more aggressively "
            "away from directional runaway and prevent deep inventory traps."
        )

    if liquidated_tickers:
        details = ", ".join([f"`{t}` (${loss:+.2f})" for t, loss in liquidated_tickers])
        recommendations.append(
            f"**Late-Game / Cutoff Liquidation Drawdown**: Heavy drawdowns occurred in {details}. "
            "In live sporting events where the underlying score changes rapidly, inventory accumulated during mid-game swings "
            "can suffer sharp liquidation penalties at market close. Consider widening the expiration cutoff "
            "(`MIN_TIME_TO_CLOSE_SECONDS`) or tightening max inventory thresholds during the 4th quarter."
        )

    # 4. Order velocity & fill ratio
    if orders_total > 5000:
        recommendations.append(
            f"**High Order Velocity ({orders_total:,} orders)**: Extremely high order cancel/replace rate. "
            "Add midpoint deadband filtering to reduce exchange API churn."
        )
    elif fills_total > 0 and orders_total > 0:
        fill_ratio = (fills_total / orders_total) * 100.0
        if fill_ratio < 2.0:
            recommendations.append(
                f"**Low Fill-to-Quote Ratio ({fill_ratio:.1f}%: {fills_total} fills / {orders_total:,} orders)**: "
                "The bot is generating a large volume of quote updates per executed fill. Consider adding quote replacement "
                "tolerance (e.g., skip cancel/replace if midpoint moved by < $0.01) to preserve API rate limits and reduce noise."
            )

    # 5. Default healthy state
    if not recommendations:
        recommendations.append(
            "**Well-Calibrated Execution**: Order quoting velocity, inventory distribution, and fee drag remained "
            "within expected risk boundaries throughout the session."
        )

    return recommendations


def format_markdown_report(metrics: Dict[str, Any]) -> str:
    """Formats the session metrics into a polished GitHub-flavored Markdown report in USD ($)."""
    target_date = metrics.get("date", "Unknown Date")
    tz_name = metrics.get("timezone", "America/New_York")
    totals = metrics.get("totals", {})
    tickers = metrics.get("tickers", {})
    recommendations = generate_recommendations(metrics)

    realized_cents = totals.get("total_realized_delta_cents", 0.0)
    unrealized_delta_cents = totals.get("total_unrealized_delta_cents", 0.0)
    ending_unrealized_cents = totals.get("total_ending_unrealized_cents", 0.0)
    fees_cents = totals.get("total_fees_delta_cents", 0.0)
    net_pnl_cents = totals.get("net_strategy_pnl_cents", 0.0)

    # Dollar conversions
    realized_dlr = realized_cents / 100.0
    unrealized_delta_dlr = unrealized_delta_cents / 100.0
    ending_unrealized_dlr = ending_unrealized_cents / 100.0
    fees_dlr = fees_cents / 100.0
    net_pnl_dlr = net_pnl_cents / 100.0

    fills = totals.get("total_fills", 0)
    orders = totals.get("total_orders", 0)
    fill_rate = (fills / orders * 100.0) if orders > 0 else 0.0

    total_rts = totals.get("total_round_trips", 0)
    wins = totals.get("winning_trades", 0)
    losses = totals.get("losing_trades", 0)
    scratches = totals.get("scratch_trades", 0)
    win_rate = totals.get("win_rate_pct", 0.0)
    win_rate_ex = totals.get("win_rate_ex_scratches_pct", 0.0)

    gross_wins_dlr = totals.get("gross_wins_cents", 0.0) / 100.0
    gross_losses_dlr = totals.get("gross_losses_cents", 0.0) / 100.0
    net_gross_realized_dlr = totals.get("net_gross_realized_cents", 0.0) / 100.0
    avg_win_dlr = totals.get("avg_win_cents", 0.0) / 100.0
    avg_loss_dlr = totals.get("avg_loss_cents", 0.0) / 100.0
    profit_factor = totals.get("profit_factor", 0.0)

    lines: List[str] = [
        f"# 📊 Daily Trading Performance Report: {target_date}",
        f"**Session Timezone**: `{tz_name}` ({metrics.get('start_local')} to {metrics.get('end_local')})",
        f"**UTC Window**: `{metrics.get('start_utc')}` to `{metrics.get('end_utc')}`",
        f"**Active Markets**: {totals.get('active_market_count', 0)} | **Total Quoting Snapshots**: {totals.get('snapshot_count', 0):,}",
        "",
        "---",
        "",
        "## 1. Executive Summary & Financial Accounting",
        "",
        "| Metric | Value ($) | Accounting Status |",
        "| :--- | :---: | :--- |",
        f"| **Session Realized PnL** | `${realized_dlr:+.2f}` | Locked-in round-trip cash delta ($\\Delta \\text{{Realized}}$) |",
        f"| **Session Unrealized PnL** | `${unrealized_delta_dlr:+.2f}` | Mark-to-Market delta ($\\Delta \\text{{Unrealized}}$) |",
        f"| **Ending Unrealized Value** | `${ending_unrealized_dlr:+.2f}` | Open inventory value at close |",
        f"| **Total Session Fees Paid** | `${fees_dlr:.2f}` | Kalshi exchange transaction costs |",
        f"| **Net Strategy PnL** | `${net_pnl_dlr:+.2f}` | $\\Delta \\text{{Realized}} + \\Delta \\text{{Unrealized}}$ |",
        "",
        "> [!NOTE]",
        f"> **Financial Invariant Check**: Total Strategy PnL reconciles strictly: "
        f"$\\Delta \\text{{Realized}} (${realized_dlr:+.2f}) + \\Delta \\text{{Unrealized}} (${unrealized_delta_dlr:+.2f}) = ${net_pnl_dlr:+.2f}$.",
        "",
        "### Why Realized PnL Ended Where It Did",
        "",
    ]

    # Explain Realized PnL
    if realized_cents > 0:
        lines.append(
            f"- **Net Profit Driver**: Realized PnL finished positive at **`${realized_dlr:+.2f}`**. "
            f"Gross spread capture from completed two-sided round trips exceeded exchange fees and inventory exit friction."
        )
    elif realized_cents < 0:
        residual_recon_dlr = (realized_cents - totals.get("net_gross_realized_cents", 0.0) + fees_cents) / 100.0
        lines.append(
            f"- **Net Drawdown Driver**: Realized PnL finished negative at **`${realized_dlr:+.2f}`**. "
            f"The session was impacted by three distinct factors: gross adverse selection on directional market moves "
            f"(**`${net_gross_realized_dlr:+.2f}`**), mandatory exchange transaction fees (**`${fees_dlr:.2f}`**), and residual "
            f"mark-to-market reconciliation adjustments (**`${residual_recon_dlr:+.2f}`**)."
        )
    else:
        lines.append(
            "- **Flat Session**: Realized PnL finished unchanged at **`$0.00`** (no completed round trips or balanced liquidity)."
        )

    if fees_cents > 0:
        pct_of_realized = (fees_cents / abs(realized_cents) * 100.0) if realized_cents != 0 else 0.0
        lines.append(
            f"- **Exchange Fee Impact**: Kalshi transaction fees totaled **`${fees_dlr:.2f}`**, "
            f"representing `{pct_of_realized:.1f}%` of the absolute realized PnL."
        )
    else:
        lines.append("- **Exchange Fee Impact**: $0.00 fees incurred during this session.")

    lines.extend([
        "",
        "---",
        "",
        "## 2. Win Rate Breakdown",
        "",
        "| Performance Metric | Session Value | Description |",
        "| :--- | :---: | :--- |",
        f"| **Total Completed Round Trips** | `{total_rts}` | Fully matched FIFO entry and exit executions |",
        f"| **Winning Trades (Profit)** | `{wins}` ({win_rate:.1f}%) | Exits executed at favorable half-spread or better |",
        f"| **Losing Trades (Loss)** | `{losses}` ({(losses/total_rts*100.0) if total_rts else 0.0:.1f}%) | Exits executed following adverse price moves or liquidation |",
        f"| **Scratch Trades (Break-even)** | `{scratches}` ({(scratches/total_rts*100.0) if total_rts else 0.0:.1f}%) | Flat round trips at identical entry and exit prices |",
        f"| **Win Rate (Excluding Scratches)** | `{win_rate_ex:.1f}%` | $\\text{{Wins}} / (\\text{{Wins}} + \\text{{Losses}})$ |",
        "",
        "### Why the Win Rate Ended Where It Did",
        "",
    ])

    if total_rts > 0:
        if win_rate >= 50.0:
            lines.append(
                f"- **Spread Capture Dominance**: The win rate finished strong at **`{win_rate:.1f}%`** "
                f"({wins} wins, {losses} losses, {scratches} scratch), indicating effective two-sided inventory turnover "
                "where quotes were regularly matched at favorable spreads."
            )
        else:
            lines.append(
                f"- **Directional Drift & Adverse Selection**: The win rate finished at **`{win_rate:.1f}%`** "
                f"({wins} wins, {losses} losses, {scratches} scratch). During periods of strong directional momentum, "
                "resting quotes on the contra-trend side are filled before orderbook repricing can occur, "
                "forcing round trips to be closed at less favorable levels."
            )
        lines.append(
            f"- **Payoff Ratio Dynamics**: Winning trades averaged **`${avg_win_dlr:+.2f}`**, "
            f"whereas losing trades averaged **`${avg_loss_dlr:+.2f}`**."
        )
    else:
        lines.append("- **No Round Trips**: Quoting was purely passive without executed fills.")

    lines.extend([
        "",
        "---",
        "",
        "## 3. Trade Outcomes Breakdown",
        "",
        "| Trade Outcome Metric | Session Value | Description |",
        "| :--- | :---: | :--- |",
        f"| **Total Quoting Orders Placed** | `{orders:,}` | {totals.get('total_buy_orders', 0):,} BIDs, {totals.get('total_sell_orders', 0):,} ASKs |",
        f"| **Executed Fills** | `{fills}` ({fill_rate:.1f}%) | Fill-to-quote conversion rate |",
        f"| **Total Cancellations / Replacements** | `{totals.get('total_cancels', 0):,}` | Cancelled resting orders during price updates |",
        f"| **Profit Factor** | `{profit_factor:.2f}` | $\\text{{Gross Wins}} / |\\text{{Gross Losses}}|$ |",
        f"| **Gross Trading Profit (Wins)** | `${gross_wins_dlr:+.2f}` | Cumulative gains across all winning round trips |",
        f"| **Gross Trading Loss (Losses)** | `${gross_losses_dlr:+.2f}` | Cumulative losses across all losing round trips |",
        f"| **Net Gross Realized (Pre-Fees)** | `${net_gross_realized_dlr:+.2f}` | Gross spread capture before exchange fees |",
        f"| **Average Winning Trade** | `${avg_win_dlr:+.2f}` | Typical half-spread capture per winning round trip |",
        f"| **Average Losing Trade** | `${avg_loss_dlr:+.2f}` | Average loss per adverse move or liquidation slice |",
        "",
        "### Why Trade Outcomes Ended Where They Did",
        "",
        f"- **Quoting Velocity vs Fill Ratio**: Quoted {orders:,} orders across the session "
        f"({totals.get('total_buy_orders', 0):,} BIDs, {totals.get('total_sell_orders', 0):,} ASKs), "
        f"yielding {fills} executed fills ({fill_rate:.1f}% fill conversion rate) and "
        f"{totals.get('total_cancels', 0):,} cancellations or quote replacements.",
    ])

    if profit_factor >= 1.0:
        lines.append(
            f"- **Positive Profit Factor ({profit_factor:.2f})**: Gross trading profits (${gross_wins_dlr:+.2f}) "
            f"exceeded gross losses (${gross_losses_dlr:+.2f}), indicating positive structural edge before fees."
        )
    elif profit_factor > 0:
        lines.append(
            f"- **Sub-Unit Profit Factor ({profit_factor:.2f})**: Cumulative gross losses (${gross_losses_dlr:+.2f}) "
            f"outweighed gross trading profits (${gross_wins_dlr:+.2f}), reflecting adverse moves or inventory offloading friction."
        )

    liquidated_tickers = [
        t for t, d in tickers.items()
        if d.get("realized_delta_cents", 0.0) < -50.0 and (abs(d.get("min_inventory", 0)) >= 4 or abs(d.get("max_inventory", 0)) >= 4)
    ]
    if liquidated_tickers:
        t_names = ", ".join([f"`{t}`" for t in liquidated_tickers])
        lines.append(
            f"- **Collar & De-risking Impact**: Significant inventory offloading occurred in {t_names}, "
            "where safety collars or stop-loss mechanisms intervened to protect against total settlement-at-zero wipeout."
        )

    lines.extend([
        "",
        "---",
        "",
        "## 4. Market-by-Market Performance Breakdown",
        "",
        "| Market Ticker | Realized PnL ($) | Fees Paid ($) | Net PnL ($) | Inv Range [Min, Max] | Orders Placed | Fills | Win Rate |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])

    if tickers:
        for ticker, t in sorted(tickers.items()):
            t_real_dlr = t["realized_delta_cents"] / 100.0
            t_fees_dlr = t["fees_delta_cents"] / 100.0
            t_net_dlr = t["net_pnl_cents"] / 100.0
            t_outcomes = t.get("trade_outcomes", {})
            t_wr = f"{t_outcomes.get('win_rate_pct', 0.0):.1f}%" if t_outcomes.get("total_round_trips", 0) > 0 else "N/A"
            inv_str = f"[{t['min_inventory']}, +{t['max_inventory']}]"
            lines.append(
                f"| `{ticker}` | `${t_real_dlr:+.2f}` | `${t_fees_dlr:.2f}` | `${t_net_dlr:+.2f}` | `{inv_str}` | {t['total_orders']:,} | {t.get('fills', 0)} | {t_wr} |"
            )
    else:
        lines.append("| *No active markets recorded during this period* | - | - | - | - | - | - | - |")

    lines.extend([
        "",
        "---",
        "",
        "## 5. Actionable Algorithmic Recommendations",
        "",
    ])

    for rec in recommendations:
        lines.append(f"- {rec}")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate daily trading performance analysis report.")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (defaults to yesterday in target timezone).",
    )
    parser.add_argument(
        "--tz",
        type=str,
        default="America/New_York",
        help="Timezone for the 24-hour trading session (default: America/New_York).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional file path to write Markdown report.",
    )
    parser.add_argument("--db-host", type=str, default=DEFAULT_DB_HOST, help="PostgreSQL host.")
    parser.add_argument("--db-port", type=int, default=DEFAULT_DB_PORT, help="PostgreSQL port.")
    parser.add_argument("--db-name", type=str, default=DEFAULT_DB_NAME, help="PostgreSQL database name.")
    parser.add_argument("--db-user", type=str, default=DEFAULT_DB_USER, help="PostgreSQL user.")
    parser.add_argument("--db-password", type=str, default=DEFAULT_DB_PASSWORD, help="PostgreSQL password.")

    args = parser.parse_args()

    # Determine target date in local timezone
    tz = zoneinfo.ZoneInfo(args.tz)
    now_local = datetime.datetime.now(tz)
    if args.date:
        try:
            target_date = datetime.date.fromisoformat(args.date)
        except ValueError:
            print(f"Error: Invalid date format {args.date!r}. Expected YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)
    else:
        target_date = (now_local - datetime.timedelta(days=1)).date()

    try:
        conn = get_db_connection(
            host=args.db_host,
            port=args.db_port,
            dbname=args.db_name,
            user=args.db_user,
            password=args.db_password,
        )
    except Exception as e:
        print(f"Error connecting to PostgreSQL database at {args.db_host}:{args.db_port}/{args.db_name}: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        metrics = fetch_daily_metrics(conn, target_date, tz_name=args.tz)
        report = format_markdown_report(metrics)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"Daily performance report written to: {args.output}")
        else:
            print(report)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
