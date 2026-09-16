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
| | **Player Props** | `KXNFLANYTD`<br>`KXNFLPASSYDS`<br>`KXNFLRUSHYDS`<br>`KXNFLRECYDS`<br>`KXNFLPASSTD` | • Anytime Touchdown Scorer<br>• Quarterback Passing Yards<br>• Running Back Rushing Yards<br>• Receiver Receiving Yards<br>• Passing Touchdowns Over/Under |
| **NBA** | **Game Lines** | `KXNBAGAME`<br>`KXNBASPREAD`<br>`KXNBATOTAL` | • Moneyline (Game Winner)<br>• Point Spread<br>• Game Total Over/Under |
| | **Player Props** | `KXNBAPTS`<br>`KXNBAREB`<br>`KXNBAAST`<br>`KXNBA3PT`<br>`KXNBAPRA` | • Player Points Over/Under<br>• Player Rebounds Over/Under<br>• Player Assists Over/Under<br>• Player Made 3-Pointers<br>• Points + Rebounds + Assists Combo |
| **MLB** | **Game Lines** | `KXMLBGAME`<br>`KXMLBRUNLINE`<br>`KXMLBTOTAL` | • Moneyline (Game Winner)<br>• Run Line (+/- 1.5 runs)<br>• Total Runs Over/Under |
| | **Player Props** | `KXMLBSTRIKEOUT`<br>`KXMLBHR`<br>`KXMLBHITS`<br>`KXMLBTOTALBASES` | • Pitcher Strikeouts Over/Under<br>• Player to Hit a Home Run<br>• Player Total Hits<br>• Player Total Bases |


## 2. Annual Calendar Priority Matrix

The router inspects the current UTC month to determine active league priorities:

| Month | Active Sports Phase | Priority 1 | Priority 2 | Priority 3 | Behavior if Dormant |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Jan** | NFL Playoffs / NBA Midseason | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLANYTD`, `KXNFLPASSYDS`, `KXNFLRUSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTD` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |
| **Feb** | Super Bowl / NBA Post-All-Star | **NFL Super Bowl Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLANYTD`, `KXNFLPASSYDS`, `KXNFLRUSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTD` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |
| **Mar** | NBA Stretch Run / MLB Opening | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Opening Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | — | Idle & Retry Discovery |
| **Apr** | NBA Playoffs / MLB Opening Month | **NBA Playoffs Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | — | Idle & Retry Discovery |
| **May** | NBA Conf. Finals / MLB Reg Season | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | — | Idle & Retry Discovery |
| **Jun** | NBA Finals / MLB Reg Season | **NBA Finals Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | — | Idle & Retry Discovery |
| **Jul** | **Summer Lull:** MLB Midseason / All-Star | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | — | — | Idle & Retry Discovery |
| **Aug** | MLB Pennant Races *(NFL Preseason Excluded)* | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | — | — | Idle & Retry Discovery |
| **Sep** | NFL Kickoff / MLB Final Month | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLANYTD`, `KXNFLPASSYDS`, `KXNFLRUSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTD` | **MLB Complete Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | — | Idle & Retry Discovery |
| **Oct** | **Triple Overlap:** NFL, NBA Tip-Off, MLB World Series | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLANYTD`, `KXNFLPASSYDS`, `KXNFLRUSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTD` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | **MLB Postseason Suite:**<br>• *Lines:* `KXMLBGAME`, `KXMLBRUNLINE`, `KXMLBTOTAL`<br>• *Props:* `KXMLBSTRIKEOUT`, `KXMLBHR`, `KXMLBHITS`, `KXMLBTOTALBASES` | Idle & Retry Discovery |
| **Nov** | NFL Midseason / NBA Reg Season / MLB Ends | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLANYTD`, `KXNFLPASSYDS`, `KXNFLRUSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTD` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |
| **Dec** | NFL Playoff Push / NBA Christmas Games | **NFL Complete Suite:**<br>• *Lines:* `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`<br>• *Props:* `KXNFLANYTD`, `KXNFLPASSYDS`, `KXNFLRUSHYDS`, `KXNFLRECYDS`, `KXNFLPASSTD` | **NBA Complete Suite:**<br>• *Lines:* `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`<br>• *Props:* `KXNBAPTS`, `KXNBAREB`, `KXNBAAST`, `KXNBA3PT`, `KXNBAPRA` | — | Idle & Retry Discovery |



## 3. Routing & Selection Architecture

```mermaid
flowchart TD
    Start([Market Discovery Request]) --> RouteCheck{User Specified<br>Target Market?}
    
    RouteCheck -- "Exact Ticker (e.g. KXNFLGAME-26SEP17DETBUF)" --> Exact[Target Exact Match]
    RouteCheck -- "Specific League (e.g. NBA)" --> ManualOverride[Query Requested League Series]
    RouteCheck -- "Default / SPORTS / NFL" --> Router[SportsSeasonRouter.get_in_season_leagues]

    Router --> CheckDate[Check Current UTC Month]
    CheckDate --> Matrix[/Lookup Priority Sequence<br>e.g. Oct: 1.NFL 2.NBA 3.MLB/]

    Matrix --> NextLeague{More Leagues<br>in Priority List?}
    NextLeague -- Yes --> QuerySeries[Fetch Events for Series<br>e.g. KXNFLGAME, KXNBAGAME, KXMLBGAME]
    
    QuerySeries --> HasEvents{Active Events<br>Found?}
    HasEvents -- No --> NextLeague
    
    HasEvents -- Yes --> FilterOut[Exclude Synthetic KXMVE & Ineligible Tickers]
    FilterOut --> HorizonRank[Rank Candidates by Liquidity<br>+ Expiration Horizon ≤ 7 Days Boost]
    
    HorizonRank --> PreFlight[Pre-Flight Orderbook Verification<br>Inspect Top 10 Candidate Books]
    
    PreFlight --> HasTwoSidedQuotes{Two-Sided Quotes<br>Confirmed?}
    HasTwoSidedQuotes -- Yes --> SelectedMarket([Selected Market Locked In])
    NextLeague -- No (All Dormant) --> IdleRetry([Idle & Retry Discovery Cycle<br>No Unwanted Capital Allocation])
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
Before committing the market maker to any contract, the bot performs a lightweight REST query to `/trade-api/v2/markets/{ticker}/orderbook` on the top 10 ranked candidates:
* **Validation Criteria:** Both `len(yes_bids) > 0` and `len(no_bids) > 0`.
* **Failover:** If a candidate is dormant or has one-sided quotes, it is bypassed in favor of the next candidate. If all top 10 are unquoted, the router falls back to the next league in the seasonal cascade.
