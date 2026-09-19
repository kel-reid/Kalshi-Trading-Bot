# Architecture Decisions Document

This document records the critical architectural decisions made in the development of the Kalshi Algorithmic Market Maker Bot, outlining the problem and technical solution for each component.


## Algorithmic Quoting & Inventory Skewing
### Problem
Prediction market contracts (specifically Kalshi's binary YES/NO contracts) resolve to either $0.00$ or $1.00$ at expiration. A market maker providing dual-sided liquidity (resting bids and asks) faces significant **inventory risk**. If the bot accumulates a large net-long YES position and the market moves against it, it faces catastrophic losses. 

### Solution
The bot implements a simplified **Avellaneda-Stoikov (A-S) Pricing Model**.
*   **Reservation Price Skew:** The midpoint is skewed according to the current net inventory ($q$) and the risk-aversion parameter ($\gamma$):
    $$R = \text{MidPrice} - (q \times \gamma)$$
*   **Spread Offsets:** Quotes are placed symmetrically around the reservation price:
    $$\text{Bid} = R - \frac{\text{Spread}}{2}, \quad \text{Ask} = R + \frac{\text{Spread}}{2}$$
*   **Active Inventory Hedging:** An active inventory threshold is enforced (currently set to $\pm 5$ contracts). When exceeded, the bot halts posting new quotes in the direction of the exposure and aggressively crosses the spread on the opposite side to exit the position.


## Infrastructure: Single Host (DigitalOcean Droplet + Docker Compose)
### Problem
A 24/7 trading bot requires reliable, cheap, and low-latency virtual compute. Orchestrating a full Kubernetes cluster or serverless configuration (like AWS ECS or GCP Cloud Run) introduces substantial networking overhead, cost, and complexity that is unnecessary for a single market-making stream.

### Solution
A single **DigitalOcean Droplet** provisioned via Terraform and managed via **Docker Compose** was selected.
*   The compute is minimal (1 vCPU, 1 GB RAM, costing ~$5-6/month).
*   Services (the Python bot and the PostgreSQL database) run as containerized service boundaries on a single host.


## Persistent Storage: PostgreSQL Container
### Problem
Trading bots generate transaction logs, order ID mapping states, fill records, and historical PnL logs. Storing this information in memory risks total data loss if the bot crashes. Storing it in flat files (like JSON or CSV) introduces file-locking and serialization limits.

### Solution
A **PostgreSQL** database runs inside a container co-located with the bot.
*   A persistent Docker volume (`postgres_data`) is mapped on the droplet host to preserve data between container teardowns.
*   The database port (`5432`) binds strictly to localhost (`127.0.0.1:5432`), making it completely inaccessible to external traffic.


## Secrets Management: Doppler
### Problem
Algorithmic trading requires highly sensitive credentials (Kalshi API keys, private RSA keys to sign API requests, and Droplet deployment credentials). Storing these in `.env` files on disk, or committing dummy `.pem` private keys to version control, represents a massive security risk.

### Solution
**Doppler** serves as the single source of truth for configuration parameters and cryptographic keys.
*   The GitHub Actions workflow injects the Doppler token into the deployment environment.
*   The container is executed via `doppler run -- docker compose up -d` to centralize configuration injection at runtime. Docker Compose environment variables are still visible in container metadata, so this setup improves operational key management but is not a zero-disk/metadata-free secret delivery model.


## Network Architecture: VPC & Strict Egress Filtering
### Problem
If a trading bot VM or a third-party Python package gets compromised, attackers could attempt to scan the database, scan the private network, or exfiltrate private credentials via remote network calls.

### Solution
A strict networking model is enforced in [main.tf](../infra/main.tf):
1.  **VPC Isolation:** The VM is hosted inside a dedicated DigitalOcean VPC, separating it from general network noise.
2.  **Strict Egress Firewall:** Outbound traffic is restricted to:
    *   Port `53` (DNS)
    *   Port `443` (HTTPS/WSS to Kalshi and GitHub)
    *   Port `80` (HTTP package mirrors)
    *   Port `123` (NTP for accurate signature timestamps)
3.  **Strict Ingress Firewall:** Inbound traffic is blocked except for SSH (Port 22) restricted to approved IP ranges.


## Observability: Grafana Alloy & Grafana Cloud
### Problem
Continuous 24/7 trading requires continuous verification. Developers need to know if the bot is experiencing elevated API latencies, losing money, or throwing rate-limiting exceptions.

### Solution
Observability utilizes a decentralized scraping system:
1.  **Prometheus Client:** The Python bot exposes standard Prometheus metrics locally on port `8000` via [metrics.py](../utils/metrics.py).
2.  **Grafana Alloy:** The Grafana Alloy daemon runs on the Droplet host to scrape port `8000` and push metrics to a hosted Grafana Cloud instance.
3.  **Webhook Alerts:** Critical trading errors and system shutdowns are pushed asynchronously to Slack or Discord.


## Concurrency Model: Python Asyncio
### Problem
Trading bots must execute multiple tasks simultaneously: listening to millisecond-level WebSocket feeds (order books, execution fills), running strategy ticks, making HTTP REST requests (placing and canceling orders), and running background maintenance loops. Doing this synchronously would block execution and cause quotes to be outdated.

### Solution
The bot is designed entirely around **Python Asyncio**.
*   WebSocket client streams run concurrently with strategy evaluation cycles.
*   Synchronous operations (like writing to Postgres or executing blocking REST calls) are offloaded to secondary threads using `asyncio.to_thread` to prevent blocking the primary event loop.
*   REST API calls are throttled using an asynchronous token-bucket rate limiter.


## Operational Resiliency: State Reconciliation & Graceful Shutdowns
### Problem
Network sockets drop packets, and API connections crash. If the bot crashes or misses a WebSocket "fill" event, its local representation of its balance or inventory drifts, which could lead to incorrect pricing skew calculations. If the bot terminates suddenly, resting limit orders might remain on the Kalshi exchange book, leaving exposure open.

### Solution
Two primary safety patterns govern operational resiliency:
1.  **State Reconciliation:** The inventory manager runs a background loop every 5 minutes, query-calling the REST API to update the authoritative balance and positions, overriding any WebSocket-related drift.
2.  **State Recovery on Start:** The bot queries resting orders on startup and cancels them to start clean.
3.  **Graceful Shutdown Handler:** The bot intercepts termination signals (`SIGINT` and `SIGTERM`) using Python's `signal` module. Upon interception, it triggers a synchronous kill switch to cancel all outstanding quotes on Kalshi before exiting.


## Market Discovery: Dynamic Seasonal Sports Routing & v2 Liquidity
### Problem
Production telemetry revealed that market discovery previously targeted distant multi-year future props (e.g. `KXNFLENDSTREAK-40NYJ-2627`) that carried cumulative historical volume but had zero active resting bids/asks. This caused the bot's quoting loop to starve in an idle state. Concurrently, Kalshi's v2 REST and WebSocket APIs deliver quotes and volume as string floats (`volume_fp`, `yes_bid_dollars`, `yes_dollars_fp`), which evaluated to 0 under legacy integer parsers. Furthermore, during sports off-seasons or midweek schedule lulls (e.g. NFL Tuesdays), the bot lacked a mechanism to pivot to active leagues.

### Solution
The market discovery engine implements a complete seasonal routing and liquidity overhaul:
1.  **`SportsSeasonRouter`:** Defines an annual calendar priority matrix (Jan–Dec) that cascades through active in-season suites (Game Lines and Player Props) across NFL, NBA, and MLB, while permanently excluding low-liquidity leagues.
2.  **Expiration Horizon Multipliers:** Applies weighted expiration factors ($\le 7$ days: $3.0\times$ vs. $> 365$ days: $0.05\times$) to ensure upcoming weekly game lines outscore distant multi-year props.
3.  **Pre-Flight Live Orderbook Checks:** Queries `/trade-api/v2/markets/{ticker}/orderbook` on top candidates, immediately verifying resting two-sided quotes before committing to a contract.
4.  **Kalshi v2 Schema Compatibility:** Ingests and maintains all dollar-string and float payloads across snapshots and deltas using full floating-point precision, preserving sub-cent price levels (e.g. 32.1¢ and 32.4¢), valid settlement prices up to 100.0¢ ($1.00), and fractional contract quantities (e.g. 0.50) without truncation or level collisions.
5.  **Sub-Cent Dollar Order Precision:** `OrderManager.place_order` formats dollar prices with dynamic precision up to 4 decimals (e.g. `32.4¢` -> `"0.324"` and `32.12¢` -> `"0.3212"`), ensuring short-inventory crossing bids and long-inventory crossing asks execute at exact intended levels without truncation or round-off failure.
6.  **Auto-Rotation for Configured Exact Tickers:** When an exact ticker is targeted and subsequently excluded upon settlement, expiration, or orderbook starvation, discovery routes the excluded target to its detected league suite (`KXNFL` -> NFL, `KXNBA` -> NBA, `KXMLB` -> MLB) or seasonal sports fallback, ensuring rotation reliably secures an active replacement market.
7.  **Weekly Contract Horizon Constraint:** Enforces `MAX_EXPIRATION_DAYS = 8` across automated discovery tiers to restrict trading exclusively to near-term weekly game lines and props (Thursday through Monday Night Football). Season-long or multi-year futures (such as `KXNFLENDSTREAK`) are strictly disqualified from automated candidate pools, guaranteeing high capital velocity and eliminating months-long capital lockup.
