"""Pure functions: raw 1xbet JSON -> our own MatchSnapshot shape.

Kept separate from xbet_client.py (which only knows HTTP) and main.py (which
only knows the poll loop) so the mapping from their field names (`O1E`, `SC`,
`LI`, ...) to ours can be tested and read on its own.
"""
from __future__ import annotations

import time

from services.collector.xbet_client import (
    MONEYLINE_AWAY_T,
    MONEYLINE_DRAW_T,
    MONEYLINE_HOME_T,
    TOTAL_OVER_T,
    TOTAL_UNDER_T,
)
from shared.events import HalfScore, MatchSnapshot, MatchStatus, MoneylineOdds, TotalLine


def is_target_league(raw: dict, sport_id: int, name_filter: str) -> bool:
    if raw.get("SI") != sport_id:
        return False
    league_name = raw.get("LE") or raw.get("L") or ""
    return name_filter.lower() in league_name.lower()


def _status(sc: dict) -> MatchStatus:
    if sc.get("CPS") == "Game finished" or sc.get("I") == "Match finished":
        return "finished"
    if sc.get("I") == "Pre-game betting":
        return "upcoming"
    return "live"


def _half_scores(sc: dict) -> list[HalfScore]:
    out = []
    for entry in sc.get("PS", []):
        value = entry.get("Value", {})
        out.append(
            HalfScore(
                half=entry.get("Key", 0),
                label=value.get("NF", f"half {entry.get('Key', '?')}"),
                home_goals=value.get("S1", 0),
                away_goals=value.get("S2", 0),
            )
        )
    return out


def _moneyline(events: list[dict]) -> MoneylineOdds | None:
    prices: dict[int, float] = {}
    for e in events:
        if e.get("G") == 1 and e.get("T") in (MONEYLINE_HOME_T, MONEYLINE_DRAW_T, MONEYLINE_AWAY_T):
            prices[e["T"]] = e["C"]
    if not all(t in prices for t in (MONEYLINE_HOME_T, MONEYLINE_DRAW_T, MONEYLINE_AWAY_T)):
        return None
    return MoneylineOdds(
        home=prices[MONEYLINE_HOME_T], draw=prices[MONEYLINE_DRAW_T], away=prices[MONEYLINE_AWAY_T]
    )


def _totals(events: list[dict]) -> list[TotalLine]:
    overs: dict[float, float] = {}
    unders: dict[float, float] = {}
    for e in events:
        if e.get("T") == TOTAL_OVER_T:
            overs[e["P"]] = e["C"]
        elif e.get("T") == TOTAL_UNDER_T:
            unders[e["P"]] = e["C"]
    return [
        TotalLine(line=line, over=overs[line], under=unders[line])
        for line in sorted(set(overs) & set(unders))
    ]


def normalize_snapshot(raw: dict) -> MatchSnapshot:
    """`raw` is one element of Get1x2_VZip's Value list, or the whole Value
    object from GetGameZip — both share the same base shape; GetGameZip's
    just has a populated `E` (odds) list."""
    sc = raw.get("SC", {})
    fs = sc.get("FS", {})
    events = raw.get("E", [])

    return MatchSnapshot(
        match_id=raw["I"],
        league_id=raw["LI"],
        league_name=raw.get("LE") or raw.get("L") or "",
        home=raw.get("O1E") or raw.get("O1") or "",
        away=raw.get("O2E") or raw.get("O2") or "",
        kickoff_ts=raw.get("S", 0),
        status=_status(sc),
        period_label=sc.get("CPS", ""),
        clock_seconds=sc.get("TS") if sc.get("CPS") in ("1st half", "2nd half") else None,
        starting_in_label=sc.get("SLS", ""),
        half_scores=_half_scores(sc),
        home_goals=fs.get("S1", 0),
        away_goals=fs.get("S2", 0),
        moneyline=_moneyline(events),
        totals=_totals(events),
        fetched_at=time.time(),
    )
