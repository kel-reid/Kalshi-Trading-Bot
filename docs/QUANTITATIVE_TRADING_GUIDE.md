# Market Making, Prediction Market Microstructure, and Algorithmic Trading Systems


### Executive Overview

Quantitative market making is often misunderstood as high-frequency directional guessing. In reality, modern automated market making is a problem of **stochastic optimal control under asymmetric information and adversarial game theory**. 

A liquidity provider does not seek to predict where an asset will trade tomorrow; rather, they act as an insurance underwriter and queue buffer. By continuously posting resting, dual-sided commitments (bids and asks), the market maker facilitates immediate execution for other market participants. In exchange for providing this liquidity, they harvest the **bid-ask spread**.

However, providing continuous liquidity exposes the market maker to two existential risks:
1. **Adverse Selection (The Winner's Curse)**: Market orders are not distributed randomly. When an informed trader crosses the spread to execute against a resting quote, they do so because the resting price is stale relative to new information. Market makers are systematically filled on the wrong side of momentum.
2. **Inventory Risk & Variance Drag**: Accumulated directional inventory ($q_t \ne 0$) introduces unhedged exposure to asset volatility, tying up margin and creating balance variance that degrades the geometric compounding of capital.

On regulated prediction exchanges like **Kalshi**, these classical dynamics operate within a unique mathematical structure: **event contracts with strictly bounded price domains ($P \in [1¢, 99¢]$)**, discontinuous binary terminal payoffs ($\$0.00$ or $\$1.00$), and non-linear quadratic exchange transaction fee curves.

This guide formalizes the mathematics, market microstructure, and distributed systems architecture underpinning the **Kalshi Trading Bot**, translating theoretical stochastic calculus (Hamilton-Jacobi-Bellman dynamic programming, Poisson arrival intensities, and CARA utility) into production execution engineering.


### Table of Contents
1. [The Mathematical Rosetta Stone: Systems & Microstructure Equivalence Mapping](#1-the-mathematical-rosetta-stone-systems--microstructure-equivalence-mapping)
2. [Market Microstructure & Limit Order Book (LOB) Dynamics](#2-market-microstructure--limit-order-book-lob-dynamics)
   - [Continuous Double Auctions & Discrete Priority Queues (FIFO vs. Pro-Rata)](#continuous-double-auctions--discrete-priority-queues-fifo-vs-pro-rata)
   - [Maker vs. Taker Asymmetries & The Spread Triad](#maker-vs-taker-asymmetries--the-spread-triad)
   - [Adverse Selection, The Winner's Curse & Kyle's Lambda](#adverse-selection-the-winners-curse--kyles-lambda)
3. [The Avellaneda-Stoikov Stochastic Control Framework](#3-the-avellaneda-stoikov-stochastic-control-framework)
   - [Continuous Arithmetic Brownian Motion & Poisson Order Arrivals](#continuous-arithmetic-brownian-motion--poisson-order-arrivals)
   - [CARA Utility & Hamilton-Jacobi-Bellman (HJB) Dynamic Programming](#cara-utility--hamilton-jacobi-bellman-hjb-dynamic-programming)
   - [Analytical Derivation of the Reservation Price (Indifference Price)](#analytical-derivation-of-the-reservation-price-indifference-price)
   - [Closed-Form Dynamic Spread Solver ($\gamma, \sigma, \kappa$)](#closed-form-dynamic-spread-solver-gamma-sigma-kappa)
4. [Prediction Market Derivatives: Bounded Metric Spaces & Event Contracts](#4-prediction-market-derivatives-bounded-metric-spaces--event-contracts)
   - [Probability Space Bounding ($P \in [1¢, 99¢]$) & Terminal Singularity](#probability-space-bounding-p-in-1-99--terminal-singularity)
   - [The Fundamental YES/NO Parity Duality](#the-fundamental-yesno-parity-duality)
   - [Kalshi's Non-Linear Quadratic Fee Mechanics & The Breakeven Spread Floor](#kalshis-non-linear-quadratic-fee-mechanics--the-breakeven-spread-floor)
   - [Expiration Pin Risk, Gamma Explosions & Blackout Safeguards](#expiration-pin-risk-gamma-explosions--blackout-safeguards)
5. [Adversarial Safeguards & Real-Time Execution Control Loops](#5-adversarial-safeguards--real-time-execution-control-loops)
   - [The 1 Hz Control Loop Architecture](#the-1-hz-control-loop-architecture)
   - [Price Velocity Circuit Breakers (Fast Market Detection)](#price-velocity-circuit-breakers-fast-market-detection)
   - [Post-Fill Quiescence & Toxic Order Clustering](#post-fill-quiescence--toxic-order-clustering)
   - [Quote Flicker vs. Queue Priority: Replacement Tolerances](#quote-flicker-vs-queue-priority-replacement-tolerances)
6. [State Reconciliation, Concurrency & Financial Conservation Laws](#6-state-reconciliation-concurrency--financial-conservation-laws)
   - [The Conservation Invariant ($\text{Total PnL} \equiv \text{Realized} + \text{Unrealized}$)](#the-conservation-invariant-texttotal-pnl-equiv-textrealized--textunrealized)
   - [Eventual Consistency (WebSocket) vs. Authoritative Ground Truth (REST)](#eventual-consistency-websocket-vs-authoritative-ground-truth-rest)
   - [Zero-Leakage Shutdown & The Kill Switch Protocol](#zero-leakage-shutdown--the-kill-switch-protocol)
7. [Capital Allocation, Bankroll Growth & Drawdown Absorption](#7-capital-allocation-bankroll-growth--drawdown-absorption)
   - [Binary Contract Kelly Criterion Formulation](#binary-contract-kelly-criterion-formulation)
   - [Shard Sweeping & Collateral Reserve Rebalancing](#shard-sweeping--collateral-reserve-rebalancing)
   - [Drawdown Limits & Session Loss Control](#drawdown-limits--session-loss-control)
8. [Summary Checklist for Systems Implementation](#summary-checklist-for-systems-implementation)
9. [Curated Academic & Industry Bibliography](#curated-academic--industry-bibliography)


### 1. The Mathematical Rosetta Stone: Systems & Microstructure Equivalence Mapping

| Software / Systems Engineering Concept | Quantitative Trading / Microstructure Concept | Mathematical Formalism |
| :--- | :--- | :--- |
| **Feedback Control Loop (PID / Kalman Filter)** | Continuous Quoting & Inventory Skewing | $r(s, q, t) = s - q \gamma \sigma^2 (T - t)$ |
| **Adversarial Network Environment / Byzantine Fault** | Adverse Selection / Toxic Informed Flow | $\Delta P_{\text{post-fill}} = \lambda \cdot \text{Sign}(\text{Trade}) \cdot \text{Vol}$ |
| **Resource Contention / Buffer Capacity** | Inventory Delta Exposure & Position Limits | $\|q_t\| \le Q_{\max}, \quad q_t \in \mathbb{Z}$ |
| **Transaction Processing Overhead & Serialization Tax** | Exchange Fees, Quadratic Slippage & Breakeven Floor | $\text{Fee}(P) = c \cdot P(1 - P), \quad s^* \ge 2\lceil\text{Fee}\rceil + \epsilon$ |
| **Eventual Consistency vs. Authoritative Master** | Real-time WebSocket Orderbook vs. Authoritative REST Portfolio | $\text{State}_{t} = \text{State}_{\text{REST}} \cup \int \Delta_{\text{WS}} dt$ |
| **State Conservation Law / Double-Entry Invariant** | Total Strategy P&L Conservation Identity | $\Pi_{\text{total}} \equiv \Pi_{\text{realized}} + \Pi_{\text{unrealized}}$ |
| **Race Condition / TOCTOU (Time-of-Check to Time-of-Use)** | Latency Arbitrage & Stale Quote Sniping | $\tau_{\text{react}} > \Delta t_{\text{info-propagation}} \implies \text{Arb Loss}$ |
| **Rate Limiter / Exponential Backoff** | Post-Fill Quiescence & Cooldown Timers | $t - t_{\text{last-fill}} < \tau_{\text{quiesce}} \implies \text{Suppress Quotes}$ |
| **Discrete 0-1 Knapsack / Portfolio Selection** | Binary Allocation under Margin Constraints | $\max \sum w_i r_i \quad \text{s.t.} \quad \sum w_i C_i \le \text{Collateral}$ |
| **Graceful Degradation / Circuit Breaker Pattern** | Price Velocity Halt & Dynamic Liquidation Engine | $|\Delta S_{\Delta t}| \ge \theta_v \implies \text{CancelAll}() \land \text{Quiesce}()$ |


### 2. Market Microstructure & Limit Order Book (LOB) Dynamics

#### Continuous Double Auctions & Discrete Priority Queues (FIFO vs. Pro-Rata)
Electronic financial exchanges match buyers and sellers via continuous double auctions managed by a **Limit Order Book (LOB)**. The LOB maintains two discrete, sorted priority queues:
- **Bids (Buy Orders)**: Sorted by price descending (highest willingness to pay first), then by timestamp ascending ($t_{\text{order}}$).
- **Asks / Offers (Sell Orders)**: Sorted by price ascending (lowest willingness to sell first), then by timestamp ascending.

```
                         THE LIMIT ORDER BOOK STATE
                         
   ASKS (Offers to Sell)
   ------------------------------------------------------------------
   Price (¢)      Aggregate Size (Contracts)   Queue Allocation
   ------------------------------------------------------------------
   55¢            120 contracts                Level 2 Ask
   54¢            35 contracts                 Best Ask (L1) ◄── [Bot Ask Quote]
   ------------------------------------------------------------------
   --- SPREAD = 54¢ - 50¢ = 4¢ (Midpoint S = 52.0¢) ---
   ------------------------------------------------------------------
   50¢            25 contracts                 Best Bid (L1) ◄── [Bot Bid Quote]
   49¢            80 contracts                 Level 2 Bid
   48¢            200 contracts                Level 3 Bid
   ------------------------------------------------------------------
   BIDS (Offers to Buy)
```

In a standard **FIFO (First-In, First-Out / Price-Time Priority)** matching engine, queue position is a valuable financial asset. If 100 contracts rest at 50¢ ahead of your 10-contract bid, all 100 contracts must execute before your order fills. 

Cancelling or modifying an existing resting quote loses queue seniority and pushes the order to the tail of the priority queue. Therefore, market makers must balance price accuracy against the latency cost of queue repositioning.

#### Maker vs. Taker Asymmetries & The Spread Triad
Every trade requires two counterparties with distinct economic profiles:
1. **Liquidity Maker (Passive)**: Submits resting limit orders inside or at the LOB boundaries. The maker dictates price but sacrifices execution certainty.
2. **Liquidity Taker (Aggressive)**: Submits market or crossing limit orders that match immediately against resting volume. The taker demands execution certainty and pays for it by crossing the bid-ask spread.

The bid-ask spread $\mathcal{S} = P_{\text{ask}} - P_{\text{bid}}$ is not pure profit. Microstructure theory breaks $\mathcal{S}$ into three structural costs:

$$\mathcal{S} = \mathcal{C}_{\text{order-processing}} + \mathcal{C}_{\text{inventory-holding}} + \mathcal{C}_{\text{adverse-selection}}$$

- $\mathcal{C}_{\text{order-processing}}$: Exchange access fees, colocation, clearing overhead, and computational capital.
- $\mathcal{C}_{\text{inventory-holding}}$: The variance cost of financing and bearing the price volatility of accumulated contracts.
- $\mathcal{C}_{\text{adverse-selection}}$: The statistical loss incurred when trading against participants who possess superior information.

#### Adverse Selection, The Winner's Curse & Kyle's Lambda
In standard distributed systems, task arrivals at a worker pool are typically assumed to be exogenous. In market making, **order arrivals are endogenous and adversarial**.

Order flow is divided into:
- **Uninformed Flow (Noise Traders / Hedgers)**: Exogenous traders seeking immediate risk transfer without private alpha. Trading with noise flow earns the bid-ask spread.
- **Informed Flow (Alpha Snipers / Fast Arbitrageurs)**: Endogenous traders who detect fundamental price shifts before your bot's network socket receives the market data update.

When an informed trader buys your resting Ask at 54¢, the true equilibrium price has often already jumped to 60¢. You were filled precisely because your resting price was mispriced. This is the **Winner's Curse**: *the fact that you were filled is itself evidence that you quoted at the wrong price*.

In **Kyle’s (1985) continuous auction model**, market price impact is parameterized by **Kyle's Lambda** ($\lambda$):

$$\Delta P_t = \lambda \cdot Q_t + \eta_t, \quad \lambda = \frac{2 \text{Cov}(v, p)}{\text{Var}(Q)} = \frac{\sigma_v}{2 \sigma_u}$$

where $\sigma_v$ is fundamental asset volatility and $\sigma_u$ is noise trader volume. When informed order flow dominates or volatility spikes, $\lambda$ increases, widening the equilibrium spread necessary to prevent the market maker from being depleted of capital.


### 3. The Avellaneda-Stoikov Stochastic Control Framework

The intellectual core of this trading bot is derived from the classic **Avellaneda & Stoikov (2008)** stochastic control model for high-frequency market making.

#### Continuous Arithmetic Brownian Motion & Poisson Order Arrivals
Let the reference mid-price $S_t$ follow an arithmetic Brownian motion with instantaneous volatility $\sigma$:

$$dS_t = \sigma dW_t$$

where $W_t$ is a standard Brownian motion on $(\Omega, \mathcal{F}, \mathbb{P})$.

The market maker controls the distance of their posted quotes from the mid-price:
- Bid distance: $\delta^b = S - P^b$
- Ask distance: $\delta^a = P^a - S$

Execution arrivals for the market maker's posted orders follow two independent point processes with Poisson arrival intensities $\lambda^b(\delta^b)$ and $\lambda^a(\delta^a)$:

$$\lambda^b(\delta^b) = A e^{-\kappa \delta^b}, \quad \lambda^a(\delta^a) = A e^{-\kappa \delta^a}$$

where $A$ represents overall market trading activity and $\kappa > 0$ represents the order book liquidity density (decay rate of fill probability as quotes widen from the inside market).

#### CARA Utility & Hamilton-Jacobi-Bellman (HJB) Dynamic Programming
The market maker holds cash $X_t$ and contract inventory $q_t \in \mathbb{Z}$. Their state dynamics evolve as:

$$dX_t = (S_t + \delta^a) dN_t^a - (S_t - \delta^b) dN_t^b$$
$$dq_t = dN_t^b - dN_t^a$$

where $N_t^b, N_t^a$ are counting processes with intensities $\lambda^b, \lambda^a$.

The market maker is risk-averse over terminal wealth at trading horizon $T$, modeled using **Constant Absolute Risk Aversion (CARA)** utility:

$$\max_{\delta^a, \delta^b} \mathbb{E}\left[ -e^{-\gamma (X_T + q_T S_T)} \right]$$

where $\gamma > 0$ is the **risk-aversion parameter** (`RISK_GAMMA` in [config.py](file:///Users/kelreid/Projects/Kalshi-Trading-Bot/config.py#L44)).

Defining the value function $V(x, q, s, t) = \sup_{\delta^a, \delta^b} \mathbb{E}_t \left[ -e^{-\gamma (X_T + q_T S_T)} \right]$, dynamic programming yields the **Hamilton-Jacobi-Bellman (HJB)** equation:

$$\partial_t V + \frac{1}{2} \sigma^2 \partial_{ss} V + \max_{\delta^b} \left\{ \lambda^b(\delta^b) \left[ V(x - s + \delta^b, q + 1, s, t) - V(x, q, s, t) \right] \right\} + \max_{\delta^a} \left\{ \lambda^a(\delta^a) \left[ V(x + s + \delta^a, q - 1, s, t) - V(x, q, s, t) \right] \right\} = 0$$

subject to the terminal boundary condition $V(x, q, s, T) = -e^{-\gamma (x + q s)}$.

#### Analytical Derivation of the Reservation Price (Indifference Price)
Using the ansatz $V(x, q, s, t) = -e^{-\gamma x} e^{-\gamma \theta(s, q, t)}$, Avellaneda and Stoikov showed that the market maker evaluates quotes around a subjective **Reservation Price** (also known as the **Indifference Price**) $r(s, q, t)$:

$$r(s, q, t) \triangleq \lim_{\Delta \to 0} \frac{V(x, q, s, t) - V(x, q - 1, s, t)}{\gamma}$$

Under a second-order Taylor expansion around inventory $q = 0$, the reservation price simplifies to:

$$r(s, q, t) = s - q \gamma \sigma^2 (T - t)$$

```
                    INVENTORY SKEWING AROUND THE RESERVATION PRICE
                    
  Case 1: Balanced Inventory (q = 0)
           Bid Quote                    Midpoint S                  Ask Quote
               │                            │                            │
               ├────── Optimal Spread / 2 ──┴────── Optimal Spread / 2 ──┤
              48¢                          50¢                          52¢
                                        (r = 50¢)

  Case 2: Long Inventory (q = +8 contracts)
        Bid Quote                Res Price r           Midpoint S            Ask Quote
            │                         │                    │                     │
            ├─── Optimal Spread / 2 ──┴── Opt Spread / 2 ──┤                     │
           43¢                       45¢                  50¢                   47¢
                                  (r = 45¢)
        *Notice: The Ask quote aggressively undercuts the market inside the spread!*
```

This mathematical structure guarantees self-regulating inventory control:
- When **Long Inventory ($q > 0$)**: $r(s, q, t) < s$. The bot drops both quotes. The Ask quote drops into the spread to attract buyers and liquidate exposure, while the Bid drops further away to disincentivize further accumulation.
- When **Short Inventory ($q < 0$)**: $r(s, q, t) > s$. The bot raises both quotes, pricing its Bid aggressively to cover while lifting its Ask away from the market.

In [strategy/market_maker.py](file:///Users/kelreid/Projects/Kalshi-Trading-Bot/strategy/market_maker.py#L484-L525), this is executed on every tick:
```python
# Sizing inventory skew normalized by quote size
inventory_skew = self.inventory / quote_size
reservation_price = mid_price - (inventory_skew * self.gamma)
```

#### Closed-Form Dynamic Spread Solver ($\gamma, \sigma, \kappa$)
Solving the first-order conditions for optimal bid and ask distances $\delta^{b*}, \delta^{a*}$ yields:

$$\delta^a(s, q, t) + \delta^b(s, q, t) = s^*(s, \sigma)$$

$$s^*(s, \sigma) = \gamma \sigma^2 (T - t) + \frac{2}{\gamma} \ln\left(1 + \frac{\gamma}{\kappa}\right)$$

The optimal round-trip spread $s^*$ is the exact sum of:
1. **The Volatility / Inventory Risk Component**: $\gamma \sigma^2 (T - t)$, scaling quadratically with realized midpoint volatility $\sigma$ and remaining time horizon.
2. **The Liquidity Decay / Arrival Component**: $\frac{2}{\gamma} \ln(1 + \frac{\gamma}{\kappa})$, compensating the market maker for book depth and fill arrival intensity.

This closed-form formulation is calculated dynamically in [calculate_optimal_spread()](file:///Users/kelreid/Projects/Kalshi-Trading-Bot/strategy/dynamic_spread.py#L156-L216) using real-time realized midpoint standard deviations over sliding observation windows.


### 4. Prediction Market Derivatives: Bounded Metric Spaces & Event Contracts

#### Probability Space Bounding ($P \in [1¢, 99¢]$) & Terminal Singularity
Unlike equities ($S_t \in [0, \infty)$) or interest rate futures ($r \in \mathbb{R}$), Kalshi event contracts are cash-settled binary options whose price space is strictly bounded:

$$P_t \in [1¢, 99¢] \subset \mathbb{R}^+$$

At expiration $T$, the contract collapses discontinuously to a Bernoulli outcome:

$$\lim_{t \to T} P_t = \begin{cases} 100¢ & \text{if Event resolves TRUE (YES)} \\ 0¢ & \text{if Event resolves FALSE (NO)} \end{cases}$$

Under risk-neutral pricing, the price $P_t$ directly models the market's consensus implied probability:

$$P_t = 100 \cdot \mathbb{Q}(\text{Event} = 1 \mid \mathcal{F}_t)$$

Because price cannot breach the boundaries $[0, 100]$, the instantaneous volatility of the contract $\sigma(P)$ is state-dependent. As $P \to 0¢$ or $P \to 100¢$, absolute price variance must compress to zero. Maximum variance occurs symmetrically at $P = 50¢$.

```
                   PREDICTION MARKET vs. EQUITIES COMPARISON
                   
              Kalshi Binary Contract                   Traditional Equity (e.g., AAPL)
    Payoff   ┌────────────────────────┐              ▲ Price
      $1.00  │                  ● YES │              │            ↗
             │                        │              │        /\ /
             │                        │              │  /\   /  V
      $0.00  │   NO ●                 │              │ /  \_/
             └────────────────────────┘              └───────────────► Time
             0¢       Price (¢)    100¢              Price unbounded: [0, +∞)
             Bounded: P ∈ [1¢, 99¢]
```

#### The Fundamental YES/NO Parity Duality
Every Kalshi market features an invariant complementary asset pair:

$$P_{\text{YES}} + P_{\text{NO}} \equiv 100¢$$

This duality eliminates short-selling borrow fees and margin recalls:
- **Buying 10 contracts of NO at 40¢** is economically and mathematically identical to **selling 10 contracts of YES at 60¢**.
- An Ask on YES at 60¢ matches against a Bid on NO at 40¢ in the exchange matching engine.
- Exposure is strictly bounded: the maximum capital at risk for any position is fixed at $\$1.00$ per contract multiplied by position size, eliminating negative-tail bankruptcy.

#### Kalshi's Non-Linear Quadratic Fee Mechanics & The Breakeven Spread Floor
Standard equities and crypto exchanges charge flat percentage fees (basis points) or fixed per-share clearing fees. Kalshi implements a **quadratic fee schedule** that scales with implied probability:

$$\text{Fee}_{\text{dollars}}(P) = \text{fee\_rate} \times P \times (1.0 - P), \quad P \in [0.01, 0.99]$$

Converting to whole cents (where $p \in [1, 99]$):

$$\text{Fee}_{\text{cents}}(p) = \text{fee\_rate} \times \frac{p \cdot (100 - p)}{100}$$

```
                KALSHI QUADRATIC EXCHANGE TRANSACTION FEE CURVE
   Fee (¢)
    2.0 ¢ ───┐                  ╭──────────╮
    1.5 ¢ ───┤                ╭─╯          ╰─╮
    1.0 ¢ ───┤              ╭─╯              ╰─╮
    0.5 ¢ ───┤            ╭─╯                  ╰─╮
    0.0 ¢ ───┴────────────┴────────────────────┴────────────
             0¢          25¢       50¢        75¢          100¢
                                 Midpoint Price
```

Because Kalshi rounds fees **up to the nearest integer cent per fill leg**, transaction costs present an asymmetric hurdle for high-turnover market makers:
- At **50¢**, single-leg fee is: $\lceil 0.07 \times 50 \times 50 / 100 \rceil = \lceil 1.75¢ \rceil = 2¢$.
- Round-trip fee (buy bid + sell ask) = $2 \times 2¢ = 4¢$.
- Quoting a 1¢ or 2¢ spread near 50¢ guarantees **negative expected value ($\mathbb{E}[\Pi] < 0$)**, regardless of fill rates!

The bot enforces this constraint via [calculate_spread_floor_cents()](file:///Users/kelreid/Projects/Kalshi-Trading-Bot/strategy/dynamic_spread.py#L65-L91):

$$\text{Spread Floor} = \max\left( \text{MIN\_SPREAD}, \quad \left\lceil 2 \cdot \left\lceil \text{Fee}(S) \right\rceil + \text{Target Edge} \right\rceil \right)$$

This mathematical constraint ensures the bot never posts quotes that yield negative net expected value after exchange clearing.

#### Expiration Pin Risk, Gamma Explosions & Blackout Safeguards
In classical Black-Scholes options pricing, binary option Gamma ($\Gamma = \frac{\partial^2 V}{\partial S^2}$) diverges to infinity as $t \to T$ when the asset trades near the strike.

In prediction markets, this manifests as **terminal pin risk**:
- In the final minutes of a sporting event or economic release, a single play or decimal tick can cause price to jump instantaneously from 45¢ to 0¢ or 100¢.
- A market maker with open resting quotes during this jump will be adversely selected and filled at 45¢ immediately before settlement, absorbing a catastrophic loss.

The bot neutralizes this hazard through multi-tiered pre-settlement gating:
1. **Pre-Settlement Liquidation**: Ceases quoting and liquidates inventory in rate-limited slices 90 minutes before contract expiration (`EXPIRATION_BUFFER_MINUTES = 90`).
2. **Expiration Blackout**: When within `EXPIRATION_BLACKOUT_SECONDS` of settlement, quotes are pulled if mid-price enters the dangerous inflection collar $[15¢, 85¢]$ (`BLACKOUT_DANGER_PRICE_LOW` to `HIGH`).


### 5. Adversarial Safeguards & Real-Time Execution Control Loops

#### The 1 Hz Control Loop Architecture
The market maker operates as a hard real-time, deterministic state machine evaluated on a 1-second tick interval. Defensive checks are prioritized in order of increasing computational cost:

```
                          1 HZ CONTROL LOOP EXECUTION FLOW
                          
   [ Master Tick Arrival (1 Hz) ]
                 │
                 ▼
     1. Market Open & Settlement Check? ──────── (No) ──> Cancel Quotes & Rotate Slate
                 │ (Yes)
                 ▼
     2. Book Two-Sided (Bid & Ask exist)? ────── (No) ──> Pull Quotes & Await Liquidity
                 │ (Yes)
                 ▼
     3. Risk Gating (Session Loss / Fees)? ───── (Hit) ─> Liquidate Position & Halt
                 │ (Pass)
                 ▼
     4. Price Velocity / Fast Market Active? ─── (Yes) ─> Pull Quotes & Quiesce (30s)
                 │ (Normal)
                 ▼
     5. Post-Fill Quiescence Window Active? ──── (Yes) ─> Yield Tick (Prevent Clustering)
                 │ (Clear)
                 ▼
     6. Avellaneda-Stoikov Pricing Engine ──────> Compute r(s, q, t) & Dynamic Spread
                 │
                 ▼
     7. Quote Replacement Tolerance Filter ─────> |P_new - P_resting| >= 1¢ ?
                 │ (Yes)
                 ▼
     8. Batch WebSocket / REST Transmission ────> Atomic Placement of Bid & Ask Quotes
```

#### Price Velocity Circuit Breakers (Fast Market Detection)
In high-volatility events, informed flow sweeps the order book in milliseconds. If the mid-price moves by $\ge 6¢$ over a rolling 20-second window (`PRICE_VELOCITY_THRESHOLD_CENTS = 6`, `PRICE_VELOCITY_WINDOW_SECONDS = 20`):
- The bot flags a **Fast Market** state.
- All resting limit quotes are purged immediately from the exchange.
- The engine enters a 30-second quiet period (`PRICE_VELOCITY_QUIESCE_SECONDS = 30`), allowing book depth to re-aggregate before reposting quotes.

#### Post-Fill Quiescence & Toxic Order Clustering
Empirical microstructure research proves that execution fills exhibit strong positive autocorrelation (autocorrelated Poisson cluster arrivals). Large institutional orders are broken up and swept through algorithms as sequential slices.

If your Bid is filled, the probability of an immediate follow-up sell order is significantly higher than baseline. To defend against multi-fill adverse selection, the bot enforces `POST_FILL_PAUSE_SECONDS = 3.0`:
- After any fill event, quoting is suspended for 3 seconds.
- This allows informed flow to exhaust itself and the orderbook midpoint to reach a new post-impact equilibrium before the bot posts fresh bids.

#### Quote Flicker vs. Queue Priority: Replacement Tolerances
In high-frequency systems, constantly updating quotes wastes REST API rate limits and forfeits FIFO queue seniority on the exchange.

The bot implements a quote replacement tolerance filter (`QUOTE_REPLACEMENT_TOLERANCE_CENTS = 1`):
$$\text{If } |P_{\text{target}} - P_{\text{resting}}| < 1.0¢ \implies \text{Retain Existing Quote}$$
Quotes are only replaced if the mathematical reservation price has shifted by at least one whole integer cent, preserving valuable FIFO queue priority.


### 6. State Reconciliation, Concurrency & Financial Conservation Laws

#### The Conservation Invariant ($\text{Total PnL} \equiv \text{Realized} + \text{Unrealized}$)
Financial accounting in a trading system requires invariant checking. In [data/inventory_manager.py](file:///Users/kelreid/Projects/Kalshi-Trading-Bot/data/inventory_manager.py), the bot maintains the strict conservation identity:

$$\Pi_{\text{total}} \equiv \Pi_{\text{realized}} + \Pi_{\text{unrealized}}$$

where:
$$\Pi_{\text{realized}} = \sum_{k=1}^N \left( P_{\text{sell}, k} - P_{\text{buy}, k} \right) \cdot Q_k - \sum_{k=1}^N \text{Fees}_k$$
$$\Pi_{\text{unrealized}} = q_{\text{net}} \cdot \left( S_{\text{mid}} - \bar{C}_{\text{basis}} \right)$$

This metric is exported to Prometheus and scraped by Grafana Alloy (`BOT_PNL_CENTS`, `KALSHI_REALIZED_PNL_CENTS`, `KALSHI_UNREALIZED_PNL_CENTS`). If any discrepancy emerges between transaction ledger logs and balance sheets, an alert is triggered immediately.

#### Eventual Consistency (WebSocket) vs. Authoritative Ground Truth (REST)
Distributed trading architectures rely on two concurrent communication planes:
- **WebSocket Feed**: Ultra-low latency, asynchronous event stream for real-time order book deltas and fill reports. Susceptible to TCP frame drops, network partitions, and desynchronization.
- **REST API**: Synchronous, point-in-time authoritative state. Higher latency, rate-limited, but idempotent and consistent.

To maintain consistency without sacrificing performance, the bot utilizes a **Dual-Track Reconciliation Pattern**:
1. Fast-path quoting operates exclusively off the local in-memory order book updated via WebSocket.
2. Every 5 minutes, a background supervisor task invokes `reconcile_inventory()` against the REST `/portfolio/positions` and `/portfolio/orders` endpoints.
3. If an undetected WebSocket disconnect caused an unrecorded fill, the background reconciler identifies the drift, forces local inventory $q_t$ to match exchange reality, and recalculates the reservation price.

#### Zero-Leakage Shutdown & The Kill Switch Protocol
The highest operational risk in automated trading is an unhandled exception that terminates the process while leaving live, unmanaged resting orders on the exchange.

The bot implements an atomic, escalating shutdown pipeline:
1. **Signal Interception**: Traps `SIGTERM` and `SIGINT` (Ctrl+C).
2. **Synchronous Kill Switch Execution**: Before the asyncio loop terminates, `KillSwitch.trigger_synchronous()` dispatches batch cancellation requests across all known order IDs.
3. **Ghost Order Sweeping**: For any order lacking a local exchange ID, the kill switch queries all open resting orders on the exchange and cancels them.
4. **Liquidation**: If inventory remains, it submits crossing market orders to liquidate the position to zero.
5. **Confirmation Gating**: The process refuses to exit with code `0` until the exchange REST API explicitly returns zero resting orders for the account.


### 7. Capital Allocation, Bankroll Growth & Drawdown Absorption

#### Binary Contract Kelly Criterion Formulation
In classical portfolio theory, the **Kelly Criterion** determines the optimal fraction $f^*$ of bankroll $W$ to commit to maximize the long-term geometric growth rate of capital:

$$g(f) = \mathbb{E}\left[ \ln(W_T) \right]$$

For a discrete binary contract purchased at price $p \in (0, 1)$ with true objective probability $p_{\text{true}} > p$:

$$f^* = \frac{p_{\text{true}} - p}{1 - p} = \frac{\text{Edge}}{1 - \text{Price}}$$

In market making, the bot captures small edges ($\epsilon = \text{Spread} / 2$) across high turnover. Under continuous approximations, the Kelly fraction becomes:

$$f^* = \frac{\mathbb{E}[R] - R_f}{\text{Var}(R)} = \frac{\mu}{\sigma^2}$$

Because prediction markets exhibit high kurtosis and parameter estimation uncertainty, full Kelly betting leads to severe drawdowns. The bot operates in a conservative **Fractional Kelly Regime ($f \approx 0.10 \cdot f^*$)**, bounding order sizes via `ORDER_DOLLARS = 1.0` and enforcing strict position limits (`MAX_HEDGE_INVENTORY`).

#### Shard Sweeping & Collateral Reserve Rebalancing
Kalshi routes certain market categories across sharded sub-exchange ledgers (e.g., Shard 0 vs. Shard 3). When trading across multiple event slates, capital can become locked in non-primary shards.

The bot features an automated intra-exchange transfer engine (`AUTO_SWEEP_SHARD0_ENABLED`):
- Monitored shards are periodically evaluated against `SHARD_RETAIN_DOLLARS`.
- Excess settled capital is automatically swept back to primary reserve shards (`SHARD0_RESERVE_DOLLARS`), preserving liquidity velocity across market rotations.

#### Drawdown Limits & Session Loss Control
To guarantee survival against structural market regime shifts or mispriced news events:
- **Session Loss Limit (`MAX_SESSION_LOSS_CENTS = 300`)**: If cumulative session realized plus unrealized losses exceed $\$3.00$, quoting stops immediately. The bot liquidates open inventory and transitions to discovery mode.
- **Session Fee Cap (`MAX_SESSION_FEES_CENTS = 250`)**: Prevents "churning"—running continuous trades that generate high volume for the exchange while earning negligible net spread.


### 8. Summary Checklist for Systems Implementation

When developing, testing, or deploying changes to this trading infrastructure, ensure all implementations comply with the following architectural checklist:

- [ ] **Mathematical Invariant**: Verify that all quote generation logic strictly references the reservation price formula: $r = s - q \gamma \sigma^2 (T - t)$.
- [ ] **Spread Floor Constraint**: Ensure dynamically generated spreads never undercut the fee breakeven floor: $s^* \ge \lceil 2 \cdot \lceil\text{Fee}(S)\rceil + \text{Target Edge}\rceil$.
- [ ] **FIFO Priority Preservation**: Never cancel and replace resting quotes unless the price change exceeds `QUOTE_REPLACEMENT_TOLERANCE_CENTS` (1¢).
- [ ] **Dual-Gating Preconditions**: Quoting loops must gate on both WebSocket order book health and REST portfolio balance verification before placing orders.
- [ ] **Adverse Selection Quiescence**: Fills must trigger an immediate quoting pause (`POST_FILL_PAUSE_SECONDS = 3.0`) to avoid clustered execution losses.
- [ ] **Zero Unconfirmed Orders**: Shutdown sequences must fail closed, re-querying the exchange to guarantee zero resting orders remain active.
- [ ] **State Conservation**: Total P&L calculations must satisfy $\Pi_{\text{total}} \equiv \Pi_{\text{realized}} + \Pi_{\text{unrealized}}$ without dropping deferred fees or basis precision.


### 9. Curated Academic & Industry Bibliography

#### Foundational Market Making Papers
1. **Avellaneda, M., & Stoikov, S. (2008)**. *High-frequency trading in a limit order book*. Quantitative Finance, 8(3), 217-224.
   - *The seminal paper deriving the Hamilton-Jacobi-Bellman formulation for reservation prices and dynamic spreads used in this codebase.*
2. **Ho, T., & Stoll, H. R. (1981)**. *Optimal dealer pricing under transactions and return uncertainty*. Journal of Financial Economics, 9(1), 47-73.
   - *The original framework introducing inventory risk and dealer reservation prices in equity markets.*
3. **Guéant, O., Tapia, C. A., & Manziadi, Z. (2012)**. *Dealing with the inventory risk: a solution to the market making problem*. Mathematics and Financial Economics, 6(4), 259-277.
   - *Provides closed-form, linearized approximations and infinite-horizon solutions to the Avellaneda-Stoikov HJB system.*
4. **Kyle, A. S. (1985)**. *Continuous auctions and informed trader*. Econometrica, 53(6), 1315-1335.
   - *Introduced Kyle's Lambda and the theoretical foundation for measuring price impact and adverse selection.*

#### Definitive Graduate Textbooks
- **Cartea, Á., Jaimungal, S., & Penalva, J. (2015)**. *Algorithmic and High-Frequency Trading*. Cambridge University Press.
  - *The canonical reference on stochastic control, trade execution algorithms (TWAP/VWAP), and market making.*
- **Bouchaud, J. P., Bonart, J., Donier, J., & Gould, M. (2018)**. *Trades, Quotes and Prices: Financial Markets Under the Microscope*. Cambridge University Press.
  - *Explores the statistical physics of limit order books, the square-root law of market impact, and order book resilience.*
- **O'Hara, M. (1995)**. *Market Microstructure Theory*. Blackwell Publishers.
  - *The essential theoretical treatise on information asymmetry, inventory control models, and exchange price discovery.*
