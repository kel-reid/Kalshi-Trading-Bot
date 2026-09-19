# Kalshi Algorithmic Market Maker Bot

[![CI/CD Pipeline](https://github.com/kel-reid/Kalshi-Trading-Bot/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/kel-reid/Kalshi-Trading-Bot/actions/workflows/ci-cd.yml)
[![codecov](https://codecov.io/gh/kel-reid/Kalshi-Trading-Bot/branch/main/graph/badge.svg?token=KkibaTfdjc)](https://codecov.io/gh/kel-reid/Kalshi-Trading-Bot)

> **Official Documentation**: For interactive architecture diagrams, seasonal routing specifications, the engineering roadmap, and setup guides, visit the **[Kalshi Trading Bot Wiki](https://github.com/kel-reid/Kalshi-Trading-Bot/wiki)**.

---

## Overview

This project is a production-grade algorithmic market-making trading bot built for the **Kalshi** prediction market exchange. It continuously provides dual-sided liquidity (bids and asks) using an asynchronous **Avellaneda-Stoikov** pricing model to capture the bid-ask spread while actively hedging inventory exposure.

### Key Capabilities
* **Avellaneda-Stoikov Pricing:** Dynamically skews reservation price based on net contract inventory (`q`) and the risk aversion parameter (`gamma`).
* **Active Inventory Hedging:** Halts adverse quoting and aggressively crosses the spread when inventory reaches +/- 5 contracts.
* **Automated Seasonal Sports Discovery:** Automatically targets high-liquidity in-season major sports contracts (NFL, NBA, MLB) with pre-flight orderbook probing and strict weekly horizon bounds (8 days or fewer).
* **Zero-Downtime Telemetry:** Emits real-time Prometheus metrics scraped by Grafana Alloy and monitored via Grafana Cloud.

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
| `TARGET_TICKER` | `string` | `""` | Target market ticker (e.g. `KXNFLGAME-26SEP21NYGLAR-NYG`), league (`NFL`), or category. Empty string triggers automated in-season discovery. |
| `ORDER_SIZE` | `integer` | `1` | Number of contracts to quote per side. |
| `MIN_SPREAD` | `integer` | `4` | Minimum profit spread required between bid and ask (in cents). |
| `RISK_GAMMA` | `float` | `0.5` | Risk-aversion parameter ($\gamma$) controlling the rate of inventory skewing. |
| `MAX_EXPIRATION_DAYS` | `float` | `8.0` | Maximum contract expiration window (days) to enforce weekly liquidity and prevent capital lockup. |
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
Selected Market: KXNFLGAME-26SEP21NYGLAR-NYG
2026-09-19 16:21:12,851 - MarketMaker - INFO - Starting Market Maker for KXNFLGAME-26SEP21NYGLAR-NYG
2026-09-19 16:21:13,234 - KalshiWS - INFO - Connected successfully.
2026-09-19 16:21:13,334 - MarketMaker - INFO - WebSocket Connected. Hydrating state...
2026-09-19 16:21:13,452 - MarketMaker - INFO - State hydrated. Beginning quoting loop.
2026-09-19 16:21:14,455 - MarketMaker - INFO - [A-S MATH] Mid=25.5c | Inventory=0 | Gamma=0.5 | ReservationPrice=25.50c | Spread=4c → Bid=23c  Ask=28c
2026-09-19 16:21:14,455 - MarketMaker - INFO - >> Placing new BID: 1 YES @ 23c
2026-09-19 16:21:14,515 - MarketMaker - INFO - >> Placing new ASK: 1 YES @ 28c
2026-09-19 16:21:18,120 - InventoryManager - INFO - Fill processed for KXNFLGAME-26SEP21NYGLAR-NYG: buy 1 yes @ 23c. New Net Pos: 1.
2026-09-19 16:21:18,589 - MarketMaker - INFO - [A-S MATH] Mid=25.5c | Inventory=1 | Gamma=0.5 | ReservationPrice=25.00c | Spread=4c → Bid=23c  Ask=27c
2026-09-19 16:21:18,590 - MarketMaker - INFO - >> Replacing ASK: 1 YES @ 27c
```

---

## Production Operations & Risk Notice

This software is an algorithmic trading system engineered for automated market making and live capital deployment on the Kalshi prediction exchange.

Algorithmic market making involves real financial exposure, execution latency sensitivities, and exchange counterparty dynamics. Operators deploying live capital should ensure:
* **Risk Calibration:** Operational risk parameters (`RISK_GAMMA`, `MIN_SPREAD`, `ORDER_SIZE`, and `MAX_EXPIRATION_DAYS`) are strictly scaled to account equity and risk limits.
* **Continuous Observability:** Production deployments maintain real-time telemetry via Grafana Cloud, Prometheus metrics, and automated Slack/Discord alerting.
* **Safety Controls:** Emergency shutdown protocols, state reconciliation routines, and synchronous kill-switch mechanisms are actively enforced to protect deployed funds.
