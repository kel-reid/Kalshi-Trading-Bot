"""
Sports Season Router & Seasonal Priority Matrix

Routes market discovery to the optimal in-season sports suites based on calendar dynamics.
Defines the full suites (game lines + player props) for NFL, NBA, and MLB.
Excludes low-liquidity leagues and penalizes distant multi-year futures.
See docs/SPORTS_SEASON_ROUTER.md for full architecture and seasonal priority calendar.
"""

import datetime
from typing import List, Optional


SPORTS_KEYWORDS = ("NFL", "MLB", "NBA", "FOOTBALL", "BASKETBALL", "BASEBALL")


class SportsSeasonRouter:
    """
    Routes market discovery to the optimal in-season sports suites based on calendar dynamics.
    Defines the full suites (game lines + player props) for NFL, NBA, and MLB.
    Excludes low-liquidity leagues and penalizes distant multi-year futures.
    See docs/SPORTS_SEASON_ROUTER.md for full architecture and seasonal priority calendar.
    """
    # Game Lines & Player Props suites per league
    NFL_GAME_LINES = ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL")
    NFL_PROPS = (
        "KXNFLTD", "KXNFLPASSYDS", "KXNFLRSHYDS", "KXNFLRECYDS", "KXNFLPASSTDS"
    )
    NFL_SERIES = NFL_GAME_LINES + NFL_PROPS

    NBA_GAME_LINES = ("KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL")
    NBA_PROPS = (
        "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA"
    )
    NBA_SERIES = NBA_GAME_LINES + NBA_PROPS

    MLB_GAME_LINES = ("KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL")
    MLB_PROPS = (
        "KXMLBKS", "KXMLBHR", "KXMLBHIT", "KXMLBTB"
    )
    MLB_SERIES = MLB_GAME_LINES + MLB_PROPS

    ALL_IN_SEASON_PREFIXES = ("KXNFL", "KXNBA", "KXMLB")

    @classmethod
    def get_in_season_leagues(cls, dt: Optional[datetime.datetime] = None) -> List[str]:
        """
        Return ordered list of active leagues ('NFL', 'NBA', 'MLB') based on calendar month.
        - Sep: NFL primary, MLB secondary (postseason race; NBA excluded)
        - Oct: Triple overlap: NFL primary, NBA secondary, MLB tertiary (World Series)
        - Nov - Feb: NFL primary, NBA secondary (MLB season concluded)
        - Mar - Jun: NBA primary (playoffs), MLB secondary (opening/regular season)
        - Jul - Aug: MLB primary (summer lull: MLB only; NFL preseason excluded)
        """
        if dt is None:
            import sys
            md = sys.modules.get("utils.market_discovery")
            dt_module = getattr(md, "datetime", datetime) if md else datetime
            dt = dt_module.datetime.now(datetime.timezone.utc)
        month = dt.month

        if month == 9:
            return ["NFL", "MLB"]
        elif month == 10:
            return ["NFL", "NBA", "MLB"]
        elif month in (11, 12, 1, 2):
            return ["NFL", "NBA"]
        elif month in (3, 4, 5, 6):
            return ["NBA", "MLB"]
        else:  # July, August (Summer lull: MLB only; NFL preseason excluded)
            return ["MLB"]

    @classmethod
    def get_primary_series_for_league(cls, league: str) -> str:
        """Return the primary game lines series ticker for a given league, or empty string if unsupported."""
        mapping = {
            "NFL": "KXNFLGAME",
            "NBA": "KXNBAGAME",
            "MLB": "KXMLBGAME",
        }
        return mapping.get(league.upper(), "")

    @classmethod
    def get_game_lines_for_league(cls, league: str) -> List[str]:
        """Return game lines series tickers for a given league."""
        mapping = {
            "NFL": list(cls.NFL_GAME_LINES),
            "NBA": list(cls.NBA_GAME_LINES),
            "MLB": list(cls.MLB_GAME_LINES),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_props_for_league(cls, league: str) -> List[str]:
        """Return player props series tickers for a given league."""
        mapping = {
            "NFL": list(cls.NFL_PROPS),
            "NBA": list(cls.NBA_PROPS),
            "MLB": list(cls.MLB_PROPS),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_series_for_league(cls, league: str) -> List[str]:
        """Return prioritized list of series tickers for a specific league, or empty list if unsupported."""
        mapping = {
            "NFL": list(cls.NFL_SERIES),
            "NBA": list(cls.NBA_SERIES),
            "MLB": list(cls.MLB_SERIES),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_in_season_series(cls, dt: Optional[datetime.datetime] = None) -> List[str]:
        """
        Return prioritized list of series tickers to query across all active in-season leagues.
        """
        leagues = cls.get_in_season_leagues(dt)
        series_list: List[str] = []
        for league in leagues:
            series_list.extend(cls.get_series_for_league(league))
        return series_list
