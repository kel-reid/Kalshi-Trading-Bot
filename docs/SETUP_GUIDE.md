# Setup Guide: Infrastructure, CI/CD & Observability

This guide provides complete instructions for provisioning a DigitalOcean Droplet, configuring automated GitHub Actions deployments, and optionally setting up telemetry with Grafana Cloud.



## Part 1: Server Provisioning & CI/CD Deployment

### Step 1: Create the Droplet
1. Log into the DigitalOcean Dashboard.
2. Click **Create** > **Droplets**.
3. **Region**: Choose **New York** (`NYC1` or `NYC3`). Kalshi's API infrastructure is hosted in AWS `us-east-1` (N. Virginia/NYC area); New York hosting minimizes network latency.
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

The GitHub Actions workflow builds the image, pushes it to GHCR, transfers `docker-compose.yml` to the Droplet, and starts the container via Doppler.



## Part 2: Observability Setup (Grafana Cloud & Alloy) *(Optional)*

The bot exposes Prometheus metrics locally on port `8000` via [metrics.py](../utils/metrics.py). [Grafana Alloy](https://grafana.com/docs/alloy/latest/) runs as a daemon on the Droplet, scraping port `8000` and pushing data to a hosted Grafana Cloud instance.

### Step 1: Install Grafana Alloy on the Server
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
2. Configure the `GRAFANA_API_KEY` for the Alloy systemd service:
   - **Option A (Systemd Environment File - Recommended):**
     Add your Grafana Cloud API key to `/etc/default/alloy` (Debian/Ubuntu) or `/etc/sysconfig/alloy` (RHEL/CentOS):
     ```bash
     echo 'GRAFANA_API_KEY="<your_grafana_cloud_api_key>"' | sudo tee -a /etc/default/alloy
     ```
   - **Option B (Direct in `/etc/alloy/config.alloy`):**
     Replace `sys.env("GRAFANA_API_KEY")` in `/etc/alloy/config.alloy` with your API token directly:
     ```alloy
     basic_auth {
       username = "3049654"
       password = "<your_grafana_cloud_api_key>"
     }
     ```
3. Restart and enable the Alloy service:
   ```bash
   sudo systemctl restart alloy
   sudo systemctl enable alloy
   ```

### Step 3: Recommended Dashboard Panels in Grafana Cloud
Create a dashboard in Grafana Cloud with the following Prometheus queries:

| Panel Title | Metric Query | Visualization | Description |
| :--- | :--- | :--- | :--- |
| **Total Orders Placed** | `sum(orders_placed_total)` | Stat | Total limit orders submitted and confirmed. |
| **Total Buy Contracts** | `sum(orders_placed_total{action="buy"})` | Stat | Total buy orders executed. |
| **Total Sell Contracts** | `sum(orders_placed_total{action="sell"})` | Stat | Total sell orders executed. |
| **Profit & Loss ($)** | `bot_pnl_cents / 100` | Time Series | Real-time bot balance in USD. |
| **Net Inventory Position** | `bot_inventory_net_position` | Time Series | Net contract exposure on active market. |
| **Kalshi API Latency** | `rate(kalshi_api_latency_seconds_sum[1m]) / rate(kalshi_api_latency_seconds_count[1m]) * 1000` | Time Series | Rolling REST execution roundtrip latency (ms). |
