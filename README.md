# Kalshi Algorithmic Market Maker

[![CI/CD Pipeline](https://github.com/kel-reid/Kalshi-Trading-Bot/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/kel-reid/Kalshi-Trading-Bot/actions/workflows/ci-cd.yml)
[![codecov](https://codecov.io/gh/kel-reid/Kalshi-Trading-Bot/branch/main/graph/badge.svg?token=KkibaTfdjc)](https://codecov.io/gh/kel-reid/Kalshi-Trading-Bot)

> **Official Documentation**: For interactive architecture diagrams, seasonal routing specifications, the engineering roadmap, and setup guides, visit the **[Kalshi Trading Bot Wiki](https://github.com/kel-reid/Kalshi-Trading-Bot/wiki)**.

---

## Overview

This project is an asynchronous algorithmic market-making trading bot built for the **Kalshi** prediction market exchange. It continuously provides dual-sided liquidity (bids and asks) using an asynchronous **Avellaneda-Stoikov** pricing model to capture the bid-ask spread while actively hedging inventory exposure.

### Key Capabilities
* **Avellaneda-Stoikov Pricing:** Dynamically skews reservation price based on net contract inventory (`q`) and the risk aversion parameter (`gamma`).
* **Active Inventory Hedging:** Dynamically halts adverse quoting and crosses the spread when net inventory breaches the hedge threshold ($5 \times \text{quote size}$, bounded by `MAX_HEDGE_INVENTORY`).
* **Execution Safeguards & Circuit Breakers:**
  * **Price Velocity Circuit Breaker (Fast Market):** Automatically detects toxic price momentum (mid-price shift $\ge 6¢$ over a 20-second rolling window) and quiesces quoting for 30 seconds.
  * **Extreme Price Collars:** Halts quoting if midpoint breaches 10¢ or 90¢ to eliminate asymmetric adverse selection near binary contract settlement bounds.
  * **Session Stop-Loss & Fee Churn:** Enforces session loss limits ($3.00) and fee caps ($2.50) before orderly liquidation and rotation.
  * **Post-Fill Adverse Selection Protection:** Pauses quoting for 3 seconds post-fill to let the orderbook stabilize.
  * **Pre-Settlement Liquidation:** Halts quoting and liquidates open inventory in slices 90 minutes prior to contract expiration.
* **Automated Seasonal Sports Discovery:** Automatically targets high-liquidity in-season major sports contracts (College Football / NCAAF, NFL, NBA, MLB) with pre-flight orderbook probing and strict weekly horizon bounds (8 days or fewer).
* **Real-Time Telemetry:** Emits live Prometheus metrics scraped by Grafana Alloy and monitored via Grafana Cloud.

---

## System Architecture

The trading bot executes as an asynchronous event-driven system on a hardened DigitalOcean Droplet:
* **Compute:** Containerized Python service with asyncio concurrency for simultaneous WebSocket orderbook feeds and REST execution.
* **Database:** Isolated PostgreSQL container recording persistent order history and execution state.
* **Observability:** Telemetry scraped on loopback port `8000` via Grafana Alloy daemon and streamed to Grafana Cloud.
* **Security:** Cryptographic RSA request signing and in-memory secret injection via Doppler.

**For the complete interactive system architecture diagram and component workflows, see the [Wiki: System Architecture](https://github.com/kel-reid/Kalshi-Trading-Bot/wiki#system-architecture).**

---

## Quick Start (Local Development)

### Prerequisites
* Python 3.12+ (tested on Python 3.12 & 3.14)
* Git

### Setup & Testing

```bash
# 1. Clone the repository
git clone git@github.com:kel-reid/Kalshi-Trading-Bot.git
cd Kalshi-Trading-Bot

# 2. Initialize virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run full test suite
pytest -v
```

For server provisioning, Docker deployment, and Doppler secret configuration, follow the **[Setup & Operations Guide](docs/SETUP_GUIDE.md)**.

---

## Configuration Parameters

The bot loads configuration parameters dynamically from environment variables or Doppler:

| Parameter | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `KALSHI_ENV` | `string` | `prod` | Exchange environment (`demo` or `prod`). |
| `TARGET_TICKER` | `string` | `""` | Target market ticker (e.g. `KXNFLGAME-26OCT04DALHOU-DAL`), league (`NFL`), or category. Empty string triggers automated in-season discovery. |
| `ORDER_SIZE` | `integer` | `1` | Fallback number of contracts to quote per side. |
| `ORDER_DOLLARS` | `float` | `1.0` | Minimum notional dollar allocation per quote for dynamic order sizing. |
| `MAX_ORDER_CONTRACTS` | `integer` | `100` | Maximum contract ceiling allowed per individual order slice. |
| `MAX_HEDGE_INVENTORY` | `integer` | `250` | Hard upper ceiling applied to the dynamic inventory hedge threshold ($5 \times \text{quote size}$). |
| `MIN_SPREAD` | `integer` | `4` | Minimum profit spread required between bid and ask (in cents). |
| `RISK_GAMMA` | `float` | `0.7` | Risk-aversion parameter (`gamma`) controlling the rate of inventory skewing. |
| `MIN_MID_PRICE` | `integer` | `10` | Lower price collar bound (cents); halts quoting when mid-price drops below this level. |
| `MAX_MID_PRICE` | `integer` | `90` | Upper price collar bound (cents); halts quoting when mid-price exceeds this level. |
| `MAX_SESSION_FEES_CENTS` | `integer` | `250` | Maximum cumulative session exchange fees (in cents) before quiesce and rotation. |
| `MAX_SESSION_LOSS_CENTS` | `integer` | `300` | Maximum cumulative session net loss (in cents) before quiesce and rotation. |
| `POST_FILL_PAUSE_SECONDS` | `float` | `3.0` | Quoting pause duration (seconds) following an execution fill for orderbook stabilization. |
| `PRICE_VELOCITY_THRESHOLD_CENTS` | `float` | `6.0` | Midpoint price shift threshold (cents) triggering the Fast Market circuit breaker. |
| `PRICE_VELOCITY_WINDOW_SECONDS` | `float` | `20.0` | Rolling observation window (seconds) evaluated for rapid price velocity shifts. |
| `PRICE_VELOCITY_QUIESCE_SECONDS` | `float` | `30.0` | Cooldown duration (seconds) to pull resting quotes and pause during fast market conditions. |
| `MAX_EXPIRATION_DAYS` | `float` | `8.0` | Maximum contract expiration window (days) to enforce weekly liquidity and prevent capital lockup. |
| `EXPIRATION_BUFFER_MINUTES` | `integer` | `90` | Expiration cutoff buffer (minutes) to cease quoting, liquidate, and rotate out before settlement. |
| `DB_HOST` | `string` | `localhost` | PostgreSQL host address (`db` inside Docker Compose). |
| `DB_PORT` | `integer` | `5432` | PostgreSQL port. |
| `DB_NAME` | `string` | `kalshi_bot` | PostgreSQL database name. |
| `DB_USER` | `string` | `postgres` | PostgreSQL username. |
| `DB_PASSWORD` | `string` | `postgres` | PostgreSQL password. |

For the seasonal matrix and series precedence rules, see the **[SportsSeasonRouter Specification](docs/SPORTS_SEASON_ROUTER.md)**.

---

## Live Output Preview

When running, the bot feeds structured telemetry and execution updates via its primary logging loop:

```text
Selected Market: KXNCAAFGAME-26OCT10INDNEB-NEB
2026-10-10 16:03:12,240 - MarketMaker - INFO - Starting Market Maker for KXNCAAFGAME-26OCT10INDNEB-NEB
2026-10-10 16:03:12,569 - KalshiWS - INFO - Connected successfully.
2026-10-10 16:03:12,643 - MarketMaker - INFO - WebSocket Connected. Hydrating state...
2026-10-10 16:03:12,773 - MarketMaker - INFO - State hydrated. Beginning quoting loop.
2026-10-10 16:03:12,841 - MarketMaker - INFO - [A-S MATH] Mid=25.5c | Size=4 | Inventory=0 | Gamma=0.7 | ReservationPrice=25.50c | Spread=4c → Bid=23c  Ask=28c | Realized=+0.0c | Unrealized=+0.0c
2026-10-10 16:03:12,842 - MarketMaker - INFO - >> Placing new BID: 4 YES @ 23c
2026-10-10 16:03:12,916 - MarketMaker - INFO - >> Placing new ASK: 4 YES @ 28c
2026-10-10 16:03:18,120 - InventoryManager - INFO - Fill processed for KXNCAAFGAME-26OCT10INDNEB-NEB: buy 4 yes @ 23c. New Net Pos: 4.
2026-10-10 16:03:18,589 - MarketMaker - INFO - [A-S MATH] Mid=25.5c | Size=4 | Inventory=4 | Gamma=0.7 | ReservationPrice=22.70c | Spread=4c → Bid=20c  Ask=25c | Realized=+0.0c | Unrealized=-10.8c
2026-10-10 16:03:18,590 - MarketMaker - INFO - >> Replacing ASK: 4 YES @ 25c
```

---

## Production Operations & Risk Notice

This software is an algorithmic trading system engineered for automated market making and live capital deployment on the Kalshi prediction exchange.

Algorithmic market making involves real financial exposure, execution latency sensitivities, and exchange counterparty dynamics. Operators deploying live capital should ensure:
* **Risk Calibration:** Operational risk parameters (`RISK_GAMMA`, `MIN_SPREAD`, `ORDER_SIZE`, and `MAX_EXPIRATION_DAYS`) are strictly scaled to account equity and risk limits.
* **Continuous Observability:** Production deployments maintain real-time telemetry via Grafana Cloud, Prometheus metrics, and automated Slack/Discord alerting.
* **Safety Controls:** Emergency shutdown protocols, state reconciliation routines, and synchronous kill-switch mechanisms are actively enforced to protect deployed funds.
