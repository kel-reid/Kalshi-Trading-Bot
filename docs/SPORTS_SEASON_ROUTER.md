# SportsSeasonRouter Specification & Architecture

## Overview
The `SportsSeasonRouter` is the automated market routing engine responsible for directing the Kalshi trading bot to the highest-liquidity sports markets throughout the calendar year.

Rather than remaining pinned to a single league or suffering orderbook starvation during off-seasons or midweek schedule lulls, the router dynamically cascades through active, in-season sports suites (Game Lines and Player Props) and verifies live two-sided orderbook depth before posting quotes.


## 1. Supported Leagues & Full Product Suites

The router supports the three dominant liquidity drivers on Kalshi: **NFL**, **NBA**, and **MLB**. 

Low-liquidity leagues and off-market sports are permanently excluded from automated routing to prevent the bot from becoming trapped in wide-spread or one-sided orderbooks.


### Full Suite by League

| League | Category | Specific Series Tickers | Description |
| :--- | :--- | :--- | :--- |
| **NFL** | **Game Lines** | `KXNFLGAME`<br>`KXNFLSPREAD`<br>`KXNFLTOTAL` | • Moneyline (Game Winner)<br>• Point Spread<br>• Game Total Over/Under |
| | **Player Props** | `KXNFLTD`<br>`KXNFLPASSYDS`<br>`KXNFLRSHYDS`<br>`KXNFLRECYDS`<br>`KXNFLPASSTDS` | • Anytime Touchdown Scorer<br>• Quarterback Passing Yards<br>• Running Back Rushing Yards<br>• Receiver Receiving Yards<br>• Passing Touchdowns Over/Under |
| **NBA** | **Game Lines** | `KXNBAGAME`<br>`KXNBASPREAD`<br>`KXNBATOTAL` | • Moneyline (Game Winner)<br>• Point Spread<br>• Game Total Over/Under |
| | **Player Props** | `KXNBAPTS`<br>`KXNBAREB`<br>`KXNBAAST`<br>`KXNBA3PT`<br>`KXNBAPRA` | • Player Points Over/Under<br>• Player Rebounds Over/Under<br>• Player Assists Over/Under<br>• Player Made 3-Pointers<br>• Points + Rebounds + Assists Combo |
| **MLB** | **Game Lines** | `KXMLBGAME`<br>`KXMLBSPREAD`<br>`KXMLBTOTAL` | • Moneyline (Game Winner)<br>• Run Line (+/- 1.5 runs)<br>• Total Runs Over/Under |
| | **Player Props** | `KXMLBKS`<br>`KXMLBHR`<br>`KXMLBHIT`<br>`KXMLBTB` | • Pitcher Strikeouts Over/Under<br>• Player to Hit a Home Run<br>• Player Total Hits<br>• Player Total Bases |


## 2. Annual Calendar Priority Matrix

The router inspects the current UTC month to determine active league priorities:

| Month | Active Sports Phase | Priority 1 | Priority 2 | Priority 3 | Behavior if Dormant |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Jan** | NFL Playoffs / NBA Midseason | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLTD`, `KXNFLPASSYDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTDS` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |
| **Feb** | Super Bowl / NBA Post-All-Star | **NFL Super Bowl Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLTD`, `KXNFLPASSYDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTDS` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |
| **Mar** | NBA Stretch Run / MLB Opening | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Opening Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | — | Idle & Retry Discovery |
| **Apr** | NBA Playoffs / MLB Opening Month | **NBA Playoffs Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | — | Idle & Retry Discovery |
| **May** | NBA Conf. Finals / MLB Reg Season | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | — | Idle & Retry Discovery |
| **Jun** | NBA Finals / MLB Reg Season | **NBA Finals Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | — | Idle & Retry Discovery |
| **Jul** | **Summer Lull:** MLB Midseason / All-Star | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | — | — | Idle & Retry Discovery |
| **Aug** | MLB Pennant Races *(NFL Preseason Excluded)* | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | — | — | Idle & Retry Discovery |
| **Sep** | NFL Kickoff / MLB Final Month | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLTD`, `KXNFLPASSYDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTDS` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | — | Idle & Retry Discovery |
| **Oct** | **Triple Overlap:** NFL, NBA Tip-Off, MLB World Series | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLTD`, `KXNFLPASSYDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTDS` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Postseason Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBSPREAD`, `KXMLBTOTAL`<br>• *Props:* `KXMLBKS`, `KXMLBHR`, `KXMLBHIT`, `KXMLBTB` | Idle & Retry Discovery |
| **Nov** | NFL Midseason / NBA Reg Season / MLB Ends | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLTD`, `KXNFLPASSYDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTDS` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |
| **Dec** | NFL Playoff Push / NBA Christmas Games | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLTD`, `KXNFLPASSYDS`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTDS` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |



## 3. Routing & Selection Architecture

```mermaid
flowchart TD
    Start([Market Discovery Request]) --> FetchMarkets[Fetch Eligible Markets Once via REST<br>fetch_eligible_markets]
    FetchMarkets --> RouteCheck{User Specified<br>Target Preference?}
    
    RouteCheck -- "Exact Ticker (e.g. KXNFLGAME-...)" --> ExactCheck{Active Exact<br>Match Found?}
    ExactCheck -- "Yes & Has Quotes" --> SelectedMarket([Selected Market Locked In])
    ExactCheck -- "Excluded / Settled / Starved" --> DetectLeague[Route to League Suite e.g. KXNFL->NFL<br>or Seasonal Fallback]
    
    RouteCheck -- "Specific League (e.g. NBA)" --> ManualOverride[Set Target League: e.g. NBA]
    RouteCheck -- "Default / SPORTS / None" --> Router[SportsSeasonRouter.get_in_season_leagues]

    DetectLeague --> Matrix[/Lookup Priority Sequence/]
    ManualOverride --> Matrix
    Router --> CheckDate[Check Current UTC Month]
    CheckDate --> Matrix

    Matrix --> NextTier{Next Tier in<br>Waterfall?}
    NextTier -- Yes --> FilterSeries[Filter In-Memory Markets for Series<br>Tier 1A Moneylines -> 1B Spreads/Totals -> Tier 2 Props]
    
    FilterSeries --> HasCandidates{Active Candidates<br>Found?}
    HasCandidates -- No --> NextTier
    
    HasCandidates -- Yes --> ExcludeIneligible[Exclude Synthetic KXMVE & Excluded Tickers]
    ExcludeIneligible --> HorizonRank[Rank Candidates by Liquidity<br>+ Expiration Horizon Multiplier]
    
    HorizonRank --> PreFlight[Pre-Flight Orderbook Verification<br>Probe up to 2 Candidates per Series<br>10 Probes Total Budget]
    
    PreFlight --> HasTwoSidedQuotes{Two-Sided Quotes<br>Confirmed?}
    HasTwoSidedQuotes -- Yes --> SelectedMarket
    HasTwoSidedQuotes -- No --> NextTier
    NextTier -- No (Exhausted / Budget Depleted) --> IdleRetry([Idle & Retry Discovery Cycle<br>No Unwanted Capital Allocation])
```


## 4. Liquidity Scoring & Horizon Multipliers

To prevent selecting multi-year distant futures with misleading historical volume (such as 2-year season props), contracts are scored with dynamic expiration multipliers:

$$\text{Score} = \left( (\text{HasQuotes} \times 1,000,000) + \text{SeriesBonus} + \text{Vol} + (\text{OI} \times 0.5) \right) \times \text{HorizonMultiplier}$$

### Expiration Horizon Weights
* **$\le 7$ Days (Upcoming this week):** **$3.0\times$**
* **$\le 14$ Days (Next week):** **$2.0\times$**
* **$\le 30$ Days (This month):** **$1.5\times$**
* **$\le 90$ Days (This season):** **$1.0\times$**
* **$\le 365$ Days (Longer term):** **$0.5\times$**
* **$> 365$ Days (Distant futures):** **$0.05\times$** (heavily discounted)

### In-Season Series Bonus
Contracts matching active league prefixes (`KXNFL`, `KXNBA`, `KXMLB`) receive a flat **+500,000** boost in the liquidity scoring formula.


## 5. Pre-Flight Live Orderbook Check
Before committing the market maker to any contract, the bot performs a lightweight REST query to `/trade-api/v2/markets/{ticker}/orderbook` to verify live liquidity:
* **Validation Criteria:** Both `len(yes_bids) > 0` and `len(no_bids) > 0`.
* **Probe Budgeting & Rate Limit Protection:**
  * **Per-Series Limit:** The router probes at most **2 candidates** per series (`DEFAULT_MAX_PROBES_PER_SERIES = 2`).
  * **Total Discovery Budget:** A shared ceiling of **10 total probes** (`DEFAULT_MAX_TOTAL_PROBES = 10`) applies across all tiers in a single discovery cycle. If the probe quota is exhausted, discovery halts cleanly to prevent REST rate-limit penalties.
* **Failover:** If a candidate is dormant or has one-sided quotes, it is bypassed in favor of the next ranked candidate in the series (up to 2 probes). If both fail, the router cascades to the next tier/series in the seasonal hierarchy.


## 6. Auto-Rotation for Configured Exact Tickers
When operators launch the bot targeting a specific contract ticker (e.g. `TARGET_TICKER="KXNFLGAME-26SEP17DETBUF"`), the bot locks onto that contract on startup. If that market reaches expiration/settlement or encounters prolonged orderbook starvation, the bot's auto-rotation mechanism calls discovery with that ticker added to `exclude_tickers`.

To prevent the bot from becoming permanently stalled:
1. **League-Specific Cascade:** If the target ticker begins with or references a supported league prefix (`KXNFL` $\rightarrow$ NFL, `KXNBA` $\rightarrow$ NBA, `KXMLB` $\rightarrow$ MLB), discovery routes directly to that league's full suite (Tier 1A moneylines, Tier 1B game lines, and Tier 2 props) to locate an active replacement within the same sport.
2. **Seasonal Fallback:** If the excluded target does not map to a recognized league prefix, discovery falls back to `SportsSeasonRouter.get_in_season_leagues()`, ensuring the bot rotates to the highest-liquidity seasonal market rather than idling indefinitely.

