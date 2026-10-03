"""
Sports Season Router & Seasonal Priority Matrix

Routes market discovery to the optimal in-season sports suites based on calendar dynamics.
Defines the full suites (game lines + player props) for NFL, NCAAF (College Football), NBA, and MLB.
Excludes low-liquidity leagues and penalizes distant multi-year futures.
See docs/SPORTS_SEASON_ROUTER.md for full architecture and seasonal priority calendar.
"""

import datetime
from typing import List, Optional

try:
    import zoneinfo
    ET_TZ = zoneinfo.ZoneInfo("America/New_York")
except (ImportError, Exception):  # pragma: no cover
    ET_TZ = datetime.timezone(datetime.timedelta(hours=-4))


SPORTS_KEYWORDS = (
    "NFL", "MLB", "NBA", "FOOTBALL", "BASKETBALL", "BASEBALL",
    "CFB", "NCAAF", "COLLEGE FOOTBALL"
)


class SportsSeasonRouter:
    """
    Routes market discovery to the optimal in-season sports suites based on calendar dynamics.
    Defines the full suites (game lines + player props) for NFL, NCAAF, NBA, and MLB.
    Excludes low-liquidity leagues and penalizes distant multi-year futures.
    See docs/SPORTS_SEASON_ROUTER.md for full architecture and seasonal priority calendar.
    """
    # Game Lines & Player Props suites per league
    NFL_GAME_LINES = ("KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL")
    NFL_PROPS = (
        "KXNFLTD", "KXNFLPASSYDS", "KXNFLRSHYDS", "KXNFLRECYDS", "KXNFLPASSTDS"
    )
    NFL_SERIES = NFL_GAME_LINES + NFL_PROPS

    NCAAF_GAME_LINES = ("KXNCAAFGAME", "KXNCAAFSPREAD", "KXNCAAFTOTAL")
    NCAAF_PROPS: tuple[str, ...] = ()
    NCAAF_SERIES = NCAAF_GAME_LINES + NCAAF_PROPS

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

    ALL_IN_SEASON_PREFIXES = ("KXNFL", "KXNBA", "KXMLB", "KXNCAAF")

    @classmethod
    def get_in_season_leagues(cls, dt: Optional[datetime.datetime] = None) -> List[str]:
        """
        Return ordered list of active leagues ('NFL', 'NCAAF', 'NBA', 'MLB') based on calendar month
        and day-of-week scheduling dynamics in US Eastern Time (ET).

        Day-of-Week Football Scheduling Dynamics (Sep - Jan):
        - Friday & Saturday: College Football (NCAAF) is the primary attraction across the country.
          NCAAF is elevated to Priority #1, followed by NFL, NBA, MLB.
        - Sunday, Monday, Thursday: NFL is live (Sunday main slate, Monday Night Football, Thursday Night Football).
          NFL retains Priority #1, followed by NCAAF, NBA, MLB.
        - Tuesday & Wednesday: Midweek football lull; standard seasonal priority applies.

        Monthly Calendar Overview:
        - Sep: NFL/NCAAF kickoff, MLB pennant chase (NBA excluded)
        - Oct: Quadruple overlap: NFL/NCAAF, NBA tip-off, MLB World Series
        - Nov - Dec: NFL playoff push, NCAAF rivalry month & bowl season/CFP, NBA reg season
        - Jan: NFL playoffs, NCAAF CFP semifinals / championship, NBA reg season
        - Feb: NFL Super Bowl, NBA reg season (NCAAF and MLB concluded)
        - Mar - Jun: NBA playoffs, MLB opening/regular season
        - Jul - Aug: Summer lull: MLB only; NFL/NCAAF preseason excluded
        """
        if dt is None:
            import sys
            md = sys.modules.get("utils.market_discovery")
            dt_module = getattr(md, "datetime", datetime) if md else datetime
            dt = dt_module.datetime.now(datetime.timezone.utc)

        # Standardize to US Eastern Time (America/New_York) to match US sports scheduling calendars
        if dt.tzinfo is None:
            dt_et = dt.replace(tzinfo=datetime.timezone.utc).astimezone(ET_TZ)
        else:
            dt_et = dt.astimezone(ET_TZ)

        month = dt_et.month
        # dt_et.weekday(): Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5, Sunday=6
        weekday = dt_et.weekday()
        is_cfb_primetime = weekday in (4, 5)  # Friday & Saturday

        if month == 9:
            return ["NCAAF", "NFL", "MLB"] if is_cfb_primetime else ["NFL", "NCAAF", "MLB"]
        elif month == 10:
            return ["NCAAF", "NFL", "NBA", "MLB"] if is_cfb_primetime else ["NFL", "NCAAF", "NBA", "MLB"]
        elif month in (11, 12, 1):
            return ["NCAAF", "NFL", "NBA"] if is_cfb_primetime else ["NFL", "NCAAF", "NBA"]
        elif month == 2:
            return ["NFL", "NBA"]
        elif month in (3, 4, 5, 6):
            return ["NBA", "MLB"]
        else:  # July, August (Summer lull: MLB only; NFL/NCAAF preseason excluded)
            return ["MLB"]

    @classmethod
    def get_primary_series_for_league(cls, league: str) -> str:
        """Return the primary game lines series ticker for a given league, or empty string if unsupported."""
        mapping = {
            "NFL": "KXNFLGAME",
            "NCAAF": "KXNCAAFGAME",
            "CFB": "KXNCAAFGAME",
            "NBA": "KXNBAGAME",
            "MLB": "KXMLBGAME",
        }
        return mapping.get(league.upper(), "")

    @classmethod
    def get_game_lines_for_league(cls, league: str) -> List[str]:
        """Return game lines series tickers for a given league."""
        mapping = {
            "NFL": list(cls.NFL_GAME_LINES),
            "NCAAF": list(cls.NCAAF_GAME_LINES),
            "CFB": list(cls.NCAAF_GAME_LINES),
            "NBA": list(cls.NBA_GAME_LINES),
            "MLB": list(cls.MLB_GAME_LINES),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_props_for_league(cls, league: str) -> List[str]:
        """Return player props series tickers for a given league."""
        mapping = {
            "NFL": list(cls.NFL_PROPS),
            "NCAAF": list(cls.NCAAF_PROPS),
            "CFB": list(cls.NCAAF_PROPS),
            "NBA": list(cls.NBA_PROPS),
            "MLB": list(cls.MLB_PROPS),
        }
        return mapping.get(league.upper(), [])

    @classmethod
    def get_series_for_league(cls, league: str) -> List[str]:
        """Return prioritized list of series tickers for a specific league, or empty list if unsupported."""
        mapping = {
            "NFL": list(cls.NFL_SERIES),
            "NCAAF": list(cls.NCAAF_SERIES),
            "CFB": list(cls.NCAAF_SERIES),
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
