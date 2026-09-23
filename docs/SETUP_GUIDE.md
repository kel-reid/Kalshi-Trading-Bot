# Setup & Operations Guide: Infrastructure, CI/CD, Droplet & Observability

This guide provides end-to-end instructions for provisioning a DigitalOcean Droplet, configuring automated GitHub Actions deployments via Doppler, running day-to-day Droplet operations, and setting up Grafana Cloud telemetry.

---

## Part 1: Server Provisioning & CI/CD Deployment

### Step 1: Create the Droplet
1. Log into the DigitalOcean Dashboard.
2. Click **Create** > **Droplets**.
3. **Region**: Choose **New York** (`NYC1` or `NYC3`). Kalshi's API infrastructure is hosted in AWS `us-east-1` (N. Virginia/NYC area); New York hosting minimizes network execution latency.
4. **OS Image**: Select **Ubuntu** (24.04 LTS or latest).
5. **Droplet Type**: Basic.
6. **CPU Options**: Regular ($6/month plan with 1GB RAM is sufficient).
7. **Authentication**: Select **SSH Key**. The private key associated with this SSH key is required for GitHub Actions authentication.
8. Click **Create Droplet** and record the public IPv4 address.

### Step 2: Install Docker on the Droplet
SSH into the server:

```bash
ssh root@<YOUR_DROPLET_IP>
```

Install Docker using the official installation script:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

Verify that the Docker service is running:

```bash
sudo systemctl status docker
```

Create the application directory:

```bash
mkdir -p ~/Kalshi-Trading-Bot
```

### Step 3: Configure GitHub Secrets
In GitHub, navigate to **Settings** > **Secrets and variables** > **Actions** > **New repository secret**, and add:

* **`DROPLET_IP`**: The public IPv4 address of the Droplet.
* **`SSH_USERNAME`**: Set to `root`.
* **`SSH_PRIVATE_KEY`**: The complete private SSH key corresponding to the public key registered on the Droplet.
* **`DOPPLER_TOKEN`**: The Doppler production service token (starts with `dp.st.prd.`) to inject runtime secrets.
* **`PAT_GHCR`**: A GitHub Personal Access Token with `read:packages` permission to pull images from GHCR (fine-grained personal access token recommended for least privilege).

### Step 4: Deploy via Git Push
Trigger the deployment pipeline by pushing code to `main`:

```bash
git push origin main
```

The GitHub Actions workflow builds the image, pushes it to GHCR, transfers `docker-compose.yml` to the Droplet, installs Doppler if missing, and launches the container stack via `doppler run -- docker compose up -d`.

---

## Part 2: Droplet Runtime Operations & Maintenance

All container management on the server should be done cleanly to avoid evaluating `${DB_PASSWORD}` without Doppler context.

### 1. View Container Execution Logs
Use direct Docker commands referencing the container name (`kalshi-bot`) to query the Docker daemon directly without parsing `docker-compose.yml`:

* **Check the selected market and discovery logs:**

  ```bash
  docker logs kalshi-bot | grep -i "Selected Market"
  ```

* **Stream live execution, order placements, and fills:**

  ```bash
  docker logs -f kalshi-bot
  ```

* **Inspect the last 100 log lines:**

  ```bash
  docker logs --tail=100 kalshi-bot
  ```

### 2. Container Lifecycle Commands
* **Restart the trading bot container:**

  ```bash
  docker restart kalshi-bot
  ```

  *(Or via Doppler: `cd ~/Kalshi-Trading-Bot && doppler run -- docker compose restart bot`)*

* **Stop the bot safely:**

  ```bash
  docker stop kalshi-bot
  ```

  *(Triggers the synchronous kill switch on SIGTERM before stopping).*

* **Teardown the full stack (Bot + Database):**

  ```bash
  cd ~/Kalshi-Trading-Bot && doppler run -- docker compose down
  ```

### 3. PostgreSQL Database Inspection
The database container (`kalshi-bot-db`) stores order execution history on a persistent host volume (`postgres_data`).

To inspect database orders directly from the Droplet:

```bash
cd ~/Kalshi-Trading-Bot
doppler run -- docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT * FROM orders ORDER BY created_at DESC LIMIT 10;"'
```

---

## Part 3: Observability Setup & Monitoring (Grafana Cloud & Alloy)

The bot exposes Prometheus metrics locally on loopback port `8000` via [`utils/metrics.py`](../utils/metrics.py). [Grafana Alloy](https://grafana.com/docs/alloy/latest/) runs as a system daemon on the Droplet, scraping port `8000` and streaming telemetry to your hosted Grafana Cloud account.

### Step 1: Install Grafana Alloy on the Droplet
SSH into the Droplet and install Alloy:

```bash
sudo apt-get update
sudo apt-get install -y apt-transport-https software-properties-common wget
sudo mkdir -p /etc/apt/keyrings/
wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor | sudo tee /etc/apt/keyrings/grafana.gpg > /dev/null
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update
sudo apt-get install grafana-alloy
```

### Step 2: Configure Alloy Credentials
1. Copy `alloy.config/config.alloy` to `/etc/alloy/config.alloy`.
2. Configure your Grafana Cloud Prometheus remote write credentials:
   - In `/etc/alloy/config.alloy`:
     - Replace `<your_grafana_cloud_prometheus_remote_write_url>` with your stack's remote-write push URL (from Grafana Cloud under **Prometheus** > **Details** / **Send Metrics**, e.g., `https://prometheus-prod-XX-prod-us-east-X.grafana.net/api/prom/push`).
     - Replace `<your_grafana_cloud_prometheus_username>` with your numeric Prometheus instance ID.
3. Configure the API key in `/etc/default/alloy`:

   ```bash
   echo 'GRAFANA_API_KEY="<your_grafana_cloud_api_key>"' | sudo tee -a /etc/default/alloy
   ```

4. Restart and enable Alloy:

   ```bash
   sudo systemctl restart alloy
   sudo systemctl enable alloy
   ```

5. Verify that Alloy is running and healthy:

   ```bash
   sudo systemctl status alloy
   journalctl -u alloy.service -n 50 --no-pager
   ```

### Step 3: Recommended Dashboard Panels in Grafana Cloud
Create a dashboard in Grafana Cloud with the following PromQL queries:

| Panel Title | Metric Query | Visualization | Description |
| :--- | :--- | :--- | :--- |
| **Total Orders Placed** | `sum(orders_placed_total)` | Stat | Total limit orders submitted and accepted by the exchange. |
| **Buy Orders Placed** | `sum(orders_placed_total{action="buy"})` | Stat | Total buy orders submitted and accepted. |
| **Sell Orders Placed** | `sum(orders_placed_total{action="sell"})` | Stat | Total sell orders submitted and accepted. |
| **Realized PnL ($)** | `kalshi_realized_pnl_cents / 100` | Stat / Time Series | Net profit/loss locked in from closed round trips minus fees. |
| **Unrealized MTM PnL ($)** | `kalshi_unrealized_pnl_cents / 100` | Stat / Time Series | Floating mark-to-market gain/loss on open lots vs orderbook mid. |
| **Total Strategy PnL ($)** | `(kalshi_realized_pnl_cents + kalshi_unrealized_pnl_cents) / 100` | Stat / Time Series | Total economic performance across open and closed inventory. |
| **Total Exchange Fees ($)** | `kalshi_total_fees_cents / 100` | Stat | Total trading transaction fees paid to Kalshi. |
| **Round Trip Trade Outcomes** | `kalshi_round_trips_total` | Bar Chart / Pie Chart | Total completed round trips categorized by profit, loss, or scratch. |
| **Cash Balance ($)** | `bot_pnl_cents / 100` | Time Series | Real-time bot cash balance in USD from exchange REST hydration. |
| **Net Inventory Position** | `bot_inventory_net_position` | Time Series | Net contract exposure on active market (`q`). |
| **Kalshi API Latency** | `rate(kalshi_api_latency_seconds_sum[1m]) / rate(kalshi_api_latency_seconds_count[1m]) * 1000` | Time Series | Rolling REST execution roundtrip latency (ms). |
