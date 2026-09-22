# Engineering Feature Roadmap: Kalshi Algorithmic Market Maker

This roadmap outlines the prioritized engineering milestones for the Kalshi Algorithmic Market Maker Bot following the operational philosophy: **Measure $\to$ Protect $\to$ Optimize $\to$ Scale**.

---

## Strategic Phases & Priority Matrix

| Priority | Feature / Capability | Category | Impact | Status |
| :--- | :--- | :--- | :--- | :--- |
| **Phase 1 (Critical)** | [Real-Time Realized PnL & Trade Attribution](#phase-1-real-time-realized-pnl--trade-attribution) | Observability & Economics | Visibility into true strategy profitability and fill quality | **Completed** |
| **Phase 2 (High)** | [Adverse Selection & Toxic Flow Protection](#phase-2-adverse-selection--toxic-flow-protection) | Risk & Capital Defense | Safeguards resting capital against rapid information jumps | **Planned** |
| **Phase 3 (Medium-High)** | [Dynamic Volatility & Adaptive Spread Modeling](#phase-3-dynamic-volatility--adaptive-spread-modeling) | Quantitative Alpha | Optimizes spread width according to real-time market regimes | **Planned** |
| **Phase 4 (Medium)** | [In-Place Order Amendment Optimization](#phase-4-in-place-order-amendment-optimization) | Execution & Latency | Minimizes unquoted windows and cuts REST API roundtrips | **Planned** |
| **Phase 5 (Low / Scale)** | [Multi-Market Portfolio Quoting Engine](#phase-5-multi-market-portfolio-quoting-engine) | Horizontal Scalability | Maximizes capital efficiency across concurrent games/leagues | **Future** |

```mermaid
graph LR
    P1["Phase 1<br/><b>Measure</b><br/>Realized PnL & Attribution"] --> P2["Phase 2<br/><b>Protect</b><br/>Toxic Flow Circuit Breaker"]
    P2 --> P3["Phase 3<br/><b>Optimize</b><br/>Dynamic Volatility & Spreads"]
    P3 --> P4["Phase 4<br/><b>Refine</b><br/>In-Place Order Amend"]
    P4 --> P5["Phase 5<br/><b>Scale</b><br/>Multi-Market Portfolio Engine"]

    style P1 fill:#1c3b2b,stroke:#2e7d32,stroke-width:2px;
    style P2 fill:#3b2d1c,stroke:#f57c00,stroke-width:2px;
    style P3 fill:#1c2d3b,stroke:#1976d2,stroke-width:2px;
    style P4 fill:#2a1c3b,stroke:#7b1fa2,stroke-width:2px;
    style P5 fill:#263238,stroke:#607d8b,stroke-width:2px;
```

---

## Phase 1: Real-Time Realized PnL & Trade Attribution

### Problem Statement
The bot persists order lifecycle records in PostgreSQL and manages inventory exposure ($q$) in memory, but lacks real-time round-trip fill attribution and dollar-denominated Profit-and-Loss (PnL) computation. Operators cannot currently assess whether market rotations, spread crossing events, or fee structures are generating positive net returns.

### Proposed Architecture & Deliverables
1. **Trade Matching & Fill Ledger**:
   - Track fills using FIFO/weighted-average cost accounting on executed contracts.
   - Separate **Realized PnL** (locked-in gain/loss upon offsetting a position) from **Unrealized PnL** (mark-to-market against live orderbook mid).
   - Account for exchange taker/maker fees where applicable.
2. **Database Persistence**:
   - Add a `trades` or `pnl_snapshots` table in PostgreSQL capturing per-market execution history:

     ```sql
     CREATE TABLE pnl_attribution (
         id SERIAL PRIMARY KEY,
         ticker VARCHAR(64) NOT NULL,
         timestamp TIMESTAMPTZ DEFAULT NOW(),
         realized_pnl_cents NUMERIC(12, 4) NOT NULL,
         unrealized_pnl_cents NUMERIC(12, 4) NOT NULL,
         total_fees_cents NUMERIC(12, 4) DEFAULT 0,
         inventory_at_snapshot INT NOT NULL,
         rotation_session_id UUID NOT NULL
     );
     ```
3. **Grafana Cloud Telemetry**:
   - Expose Prometheus gauges via `utils/metrics.py`:
     - `kalshi_realized_pnl_cents` (gauge by market ticker)
     - `kalshi_unrealized_pnl_cents` (gauge by market ticker)
     - `kalshi_cumulative_pnl_cents` (counter of net realized profit)
   - Add dedicated panels to the Grafana Cloud dashboard for instantaneous balance and PnL monitoring.

### Acceptance Criteria
- [x] Every partial and complete fill computes incremental realized PnL against existing inventory.
- [x] PnL metrics automatically reset or re-tag when auto-rotation transitions to a new contract.
- [x] Zero blocking calls on the asyncio event loop during database writes.

---

## Phase 2: Adverse Selection & Toxic Flow Protection

### Problem Statement
Sports contracts exhibit sudden probability jumps caused by live events (e.g., touchdowns, turnovers, instant replay rulings). When an informed counterparty sweeps the book, standard Avellaneda-Stoikov inventory skewing cannot react fast enough during the 1-second quoting cycle, leaving resting orders vulnerable to rapid sequential fills at stale prices.

### Proposed Architecture & Deliverables
1. **Toxic Flow Detector**:
   - Track micro-burst execution velocity: number of contracts filled within a rolling window ($< 2.0$ seconds).
   - If fills exceed an execution threshold (e.g., $\ge 3$ contracts in $\le 2$ seconds in a single direction), flag the book as experiencing toxic or informed flow.
2. **Circuit Breaker Actions**:
   - **Immediate Quote Pull**: Cancel resting orders on the affected side via fast REST or synchronous kill switch.
   - **Quote Fade**: Temporarily widen the bid/ask spread (e.g., $2\times$ or $3\times$ `MIN_SPREAD`) for a cool-down duration (e.g., 5–15 seconds).
   - **Orderbook Skew Pause**: Delay posting new orders until the book re-stabilizes with two-sided resting liquidity.
3. **Alerting & Telemetry**:
   - Increment `kalshi_toxic_flow_trips_total` metric.
   - Dispatch webhook alert when the circuit breaker trips.

### Acceptance Criteria
- [ ] Rapid fills in the same direction trigger quote pulls within 200ms.
- [ ] Bot resumes normal quoting automatically once the cooldown window expires and orderbook stability returns.
- [ ] Comprehensive unit tests simulating sweeping fills without false-positive triggers under normal volume.

---

## Phase 3: Dynamic Volatility & Adaptive Spread Modeling

### Problem Statement
The strategy currently uses a fixed static spread (`MIN_SPREAD = 4¢`). During periods of low volatility, 4¢ is unnecessarily wide, forfeiting passive fill volume. During high volatility, 4¢ is too narrow, resulting in adverse selection.

### Proposed Architecture & Deliverables
1. **Real-Time Volatility Estimator**:
   - Calculate rolling exponentially weighted moving standard deviation ($\sigma$) of the orderbook mid-price over configurable windows (e.g., 30s, 60s, 300s).
2. **Avellaneda-Stoikov Adaptive Formulation**:
   - Replace static spread with the continuous equation:
     $$\delta^a + \delta^b = \gamma \sigma^2 + \frac{2}{\gamma} \ln\left(1 + \frac{\gamma}{\kappa}\right)$$
   - Bound by safety limits: $\text{Spread} = \text{clamp}(\delta, \; \text{MIN\_SPREAD}, \; \text{MAX\_SPREAD})$.
3. **Orderbook Imbalance Integration**:
   - Adjust micro-price reservation based on top-of-book volume imbalance:
     $$I = \frac{V_{\text{bid}} - V_{\text{ask}}}{V_{\text{bid}} + V_{\text{ask}}}$$

### Acceptance Criteria
- [ ] Mid-price volatility computed incrementally in $O(1)$ time with zero performance degradation.
- [ ] Spreads dynamically expand during fast market movements and compress during tight consolidations.
- [ ] Defensive fallbacks enforce `MIN_SPREAD` if orderbook data is sparse.

---

## Phase 4: In-Place Order Amendment Optimization

### Problem Statement
When reservation prices update, the bot currently performs a sequential `cancel_order` $\to$ `place_order` REST sequence. This introduces two network round-trips (~150–300ms) and creates a brief window where the bot maintains no resting presence in the market.

### Proposed Architecture & Deliverables
1. **Amend API Integration**:
   - Integrate Kalshi’s order amend endpoint (`PUT /trade-api/v2/portfolio/orders/{order_id}/amend`) to update resting prices or quantities in a single atomic request.
2. **State Management**:
   - Update `OrderManager` to prefer atomic amendments over full cancellation whenever the contract and side remain unchanged.
   - Maintain client-side order IDs across successful amend operations.
3. **Graceful Fallbacks**:
   - Automatically fall back to standard `cancel_order` + `place_order` if the resting order has already filled, expired, or if the exchange rejects the amendment.

### Acceptance Criteria
- [ ] Order adjustments complete in a single REST roundtrip.
- [ ] Queue priority preserved on volume reductions where supported by Kalshi matching engine rules.
- [ ] Fallback paths verified with deterministic unit tests.

---

## Phase 5: Multi-Market Portfolio Quoting Engine

### Problem Statement
The bot is currently constrained to a single market (`self.ticker`) at any given time. During schedule lulls or off-peak hours, capital remains idle. When multiple high-volume games occur concurrently (e.g., NFL Sunday 1:00 PM slate), the bot cannot capture spread across the wider board.

### Proposed Architecture & Deliverables
1. **Portfolio Orchestrator**:
   - Manage $N$ concurrent `AvellanedaStoikovBot` instances (e.g., 2–4 markets).
   - Coordinate global risk limits (maximum portfolio gross contract exposure and maximum portfolio margin).
2. **Shared Connection Pooling**:
   - Route multi-market WebSocket subscriptions over a unified connection multiplexer.
   - Share the existing token-bucket REST rate limiter across all active market workers.
3. **Dynamic Capital Allocation**:
   - Allocate quote sizing dynamically based on market liquidity rank and volatility.

### Acceptance Criteria
- [ ] Multiple markets quote concurrently without WebSocket socket starvation or race conditions.
- [ ] Global portfolio kill switch cancels all active quotes across all markets simultaneously.
- [ ] Unified database logging with clear market segregation.
