# Architecture Decisions Document

This document records the critical architectural decisions made in the development of the Kalshi Algorithmic Market Maker Bot, outlining the context, decisions, and consequences of each choice.


## Algorithmic Quoting & Inventory Skewing
### Context
Prediction market contracts (specifically Kalshi's binary YES/NO contracts) resolve to either $0.00$ or $1.00$ at expiration. A market maker providing dual-sided liquidity (resting bids and asks) faces significant **inventory risk**. If the bot accumulates a large net-long YES position and the market moves against it, it faces catastrophic losses. 

### Decision
We implemented a simplified **Avellaneda-Stoikov (A-S) Pricing Model**.
*   **Reservation Price Skew:** The midpoint is skewed according to the current net inventory ($q$) and the risk-aversion parameter ($\gamma$):
    $$R = \text{MidPrice} - (q \times \gamma)$$
*   **Spread Offsets:** Quotes are placed symmetrically around the reservation price:
    $$\text{Bid} = R - \frac{\text{Spread}}{2}, \quad \text{Ask} = R + \frac{\text{Spread}}{2}$$
*   **Active Inventory Hedging:** We established an active inventory threshold (currently set to $\pm 5$ contracts). When exceeded, the bot halts posting new quotes in the direction of the exposure and aggressively crosses the spread on the opposite side to exit the position.

### Consequences
*   **Asymmetric Filling:** As inventory grows YES-heavy, the reservation price drops, raising the Ask price (making it harder to buy more) and lowering the Bid price (making it cheaper to sell YES), naturally driving inventory back to neutral.
*   **Loss Prevention:** The active hedge prevents the bot from holding massive inventory during one-sided market trends.

---

## Infrastructure: Single Host (DigitalOcean Droplet + Docker Compose)
### Context
A 24/7 trading bot requires reliable, cheap, and low-latency virtual compute. Orchestrating a full Kubernetes cluster or serverless configuration (like AWS ECS or GCP Cloud Run) introduces substantial networking overhead, cost, and complexity that is unnecessary for a single market-making stream.

### Decision
We chose a single **DigitalOcean Droplet** provisioned via Terraform and managed via **Docker Compose**.
*   The compute is minimal (1 vCPU, 1 GB RAM, costing ~$5-6/month).
*   Services (the Python bot and the PostgreSQL database) run as containerized service boundaries on a single host.

### Consequences
*   **Predictable Cost:** Low, flat monthly cost.
*   **Low Latency:** Co-locating the trading database and the execution loop on the same host loopback interface minimizes transactional network latency.
*   **Operational Simplicity:** Deployments are handled via a standard Docker pull and compose restart.

---

## Persistent Storage: PostgreSQL Container
### Context
Trading bots generate transaction logs, order ID mapping states, fill records, and historical PnL logs. Storing this information in memory risks total data loss if the bot crashes. Storing it in flat files (like JSON or CSV) introduces file-locking and serialization limits.

### Decision
We deployed a **PostgreSQL** database inside a container co-located with the bot.
*   We mapped a persistent Docker volume (`postgres_data`) on the droplet host to preserve data between container teardowns.
*   We configured the database port (`5432`) to bind strictly to localhost (`127.0.0.1:5432`), making it completely inaccessible to external traffic.

### Consequences
*   **Reliable Auditing:** ACID-compliant storage for order logs.
*   **Analytical Foundation:** Standardized tables make it easy to run SQL queries for daily PnL, slip calculations, and execution statistics.

---

## Secrets Management: Doppler
### Context
Algorithmic trading requires highly sensitive credentials (Kalshi API keys, private RSA keys to sign API requests, and Droplet deployment credentials). Storing these in `.env` files on disk, or committing dummy `.pem` private keys to version control, represents a massive security risk.

### Decision
We integrated **Doppler** as the single source of truth for configuration parameters and cryptographic keys.
*   The GitHub Actions workflow injects the Doppler token into the deployment environment.
*   The container is executed via `doppler run -- docker compose up -d` to centralize configuration injection at runtime. Docker Compose environment variables are still visible in container metadata, so this setup improves operational key management but is not a zero-disk/metadata-free secret delivery model.

### Consequences
*   **Centralized Secret Handling:** Credentials are managed from one control plane instead of being hardcoded in tracked files.
*   **Centralized Configuration:** Changing trading parameters (such as `RISK_GAMMA` or `MIN_SPREAD`) can be done dynamically from the Doppler dashboard without redeploying code.

---

## Network Architecture: VPC & Strict Egress Filtering
### Context
If a trading bot VM or a third-party Python package gets compromised, attackers could attempt to scan the database, scan the private network, or exfiltrate private credentials via remote network calls.

### Decision
We implemented a strict networking model in [main.tf](../infra/main.tf):
1.  **VPC Isolation:** The VM is hosted inside a dedicated DigitalOcean VPC, separating it from general network noise.
2.  **Strict Egress Firewall:** Outbound traffic is restricted to:
    *   Port `53` (DNS)
    *   Port `443` (HTTPS/WSS to Kalshi and GitHub)
    *   Port `80` (HTTP package mirrors)
    *   Port `123` (NTP for accurate signature timestamps)
3.  **Strict Ingress Firewall:** Inbound traffic is blocked except for SSH (Port 22) restricted to approved IP ranges.

### Consequences
*   **Exfiltration Prevention:** Even if malicious code runs inside the VM, it cannot establish connection tunnels to unauthorized IPs.
*   **Closed API Surface:** Internal endpoints (PostgreSQL `5432` and Prometheus metrics `8000`) are blocked from receiving public internet traffic.

---

## Observability: Grafana Alloy & Grafana Cloud
### Context
Continuous 24/7 trading requires continuous verification. Developers need to know if the bot is experiencing elevated API latencies, losing money, or throwing rate-limiting exceptions.

### Decision
We implemented a decentralized scraping system:
1.  **Prometheus Client:** The Python bot exposes standard Prometheus metrics locally on port `8000` via [metrics.py](../utils/metrics.py).
2.  **Grafana Alloy:** We run the Grafana Alloy daemon on the Droplet host to scrape port `8000` and push it to a hosted Grafana Cloud instance.
3.  **Webhook Alerts:** Critical trading errors and system shutdowns are pushed asynchronously to Slack or Discord.

### Consequences
*   **Rich Dashboards:** We can build dashboards visualizing PnL, inventory skews, execution latency, and error counts.
*   **Real-time Alerting:** Webhook integrations ensure developers are immediately paged if the bot crashes.

---

## Concurrency Model: Python Asyncio
### Context
Trading bots must execute multiple tasks simultaneously: listening to millisecond-level WebSocket feeds (order books, execution fills), running strategy ticks, making HTTP REST requests (placing and canceling orders), and running background maintenance loops. Doing this synchronously would block execution and cause quotes to be outdated.

### Decision
The bot is designed entirely around **Python Asyncio**.
*   WebSocket client streams run concurrently with strategy evaluation cycles.
*   Synchronous operations (like writing to Postgres or executing blocking REST calls) are offloaded to secondary threads using `asyncio.to_thread` to prevent blocking the primary event loop.
*   REST API calls are throttled using an asynchronous token-bucket rate limiter.

### Consequences
*   **Non-blocking Execution:** The WebSocket read loop continues processing orderbook snapshots even when the bot is busy posting a resting order via HTTP.
*   **I/O Isolation from Event Loop:** Offloading blocking calls with `asyncio.to_thread` keeps network and database I/O from stalling the main async trading loop.

---

## Operational Resiliency: State Reconciliation & Graceful Shutdowns
### Context
Network sockets drop packets, and API connections crash. If the bot crashes or misses a WebSocket "fill" event, its local representation of its balance or inventory drifts, which could lead to incorrect pricing skew calculations. If the bot terminates suddenly, resting limit orders might remain on the Kalshi exchange book, leaving exposure open.

### Decision
We introduced two safety patterns:
1.  **State Reconciliation:** The inventory manager runs a background loop every 5 minutes, query-calling the REST API to update the authoritative balance and positions, overriding any WebSocket-related drift.
2.  **State Recovery on Start:** The bot queries resting orders on startup and cancels them to start clean.
3.  **Graceful Shutdown Handler:** The bot intercepts termination signals (`SIGINT` and `SIGTERM`) using Python's `signal` module. Upon interception, it triggers a synchronous kill switch to cancel all outstanding quotes on Kalshi before exiting.

### Consequences
*   **Orphan Prevention:** The bot never leaves open quotes sitting in the market after the software process has terminated.
*   **Self-Healing State:** Temporary network dropouts will not permanently skew the bot's mathematical calculations.
