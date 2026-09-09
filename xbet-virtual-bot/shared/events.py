"""The event contracts every service agrees on.

Two families travel over the bus:

  MatchSnapshot   — one per (match, poll), published by the collector on
                     `channel_snapshots`. A dumb, literal read of "what does
                     the API say about this match right now." No memory of
                     what came before.

  Match*Event     — published by the aggregator on `channel_match_events`,
                     one per *transition* (discovered / started / half-time /
                     finished), each carrying only what's new to say. This is
                     what the display service (or any future consumer —
                     Telegram alerts, a DB writer, ...) actually subscribes
                     to; nobody but the aggregator should need to look at a
                     raw snapshot.

Every model is a pydantic BaseModel so it round-trips through Redis pub/sub
as JSON with validation on both ends, and so a schema mistake fails loudly
at publish/parse time instead of silently corrupting the terminal display.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

MatchStatus = Literal["upcoming", "live", "finished"]


class HalfScore(BaseModel):
    half: int  # 1 or 2
    label: str  # "1st half" / "2nd half" as the site names it
    home_goals: int
    away_goals: int


class MoneylineOdds(BaseModel):
    """The W1 / X / W2 three-way market."""

    home: float
    draw: float
    away: float
    winner: Literal["home", "draw", "away"] | None = None  # set once settled


class TotalLine(BaseModel):
    """One Over/Under row, e.g. "O/U 15.5"."""

    line: float
    over: float
    under: float
    winner: Literal["over", "under"] | None = None  # set once settled


class MatchSnapshot(BaseModel):
    kind: Literal["snapshot"] = "snapshot"
    match_id: int
    league_id: int
    league_name: str
    home: str
    away: str
    kickoff_ts: int
    status: MatchStatus
    period_label: str  # "" | "1st half" | "2nd half" | "Game finished"
    clock_seconds: int | None = None
    starting_in_label: str = ""
    half_scores: list[HalfScore] = []
    home_goals: int = 0
    away_goals: int = 0
    moneyline: MoneylineOdds | None = None
    totals: list[TotalLine] = []
    fetched_at: float


class MatchDiscovered(BaseModel):
    kind: Literal["discovered"] = "discovered"
    match_id: int
    league_name: str
    home: str
    away: str
    kickoff_ts: int
    starting_in_label: str
    moneyline: MoneylineOdds | None = None
    totals: list[TotalLine] = []


class MatchStarted(BaseModel):
    kind: Literal["started"] = "started"
    match_id: int
    league_name: str
    home: str
    away: str


class MatchHalfTime(BaseModel):
    kind: Literal["half_time"] = "half_time"
    match_id: int
    league_name: str
    home: str
    away: str
    first_half: HalfScore


class MatchScoreChanged(BaseModel):
    kind: Literal["score_changed"] = "score_changed"
    match_id: int
    league_name: str
    home: str
    away: str
    period_label: str
    clock_seconds: int | None = None
    home_goals: int
    away_goals: int
    # The live Total O/U ladder at this instant. Carried on every goal update
    # (not just Discovered/Finished) because the display's live scorecard —
    # the one place odds matter while a match is actually in progress —
    # renders straight off this event; see services/display/render.py.
    totals: list[TotalLine] = []


class MatchFinished(BaseModel):
    kind: Literal["finished"] = "finished"
    match_id: int
    league_name: str
    home: str
    away: str
    first_half: HalfScore | None = None
    second_half: HalfScore | None = None
    total_home_goals: int
    total_away_goals: int
    moneyline: MoneylineOdds | None = None
    totals: list[TotalLine] = []
    finished_at: float

    @property
    def total_goals(self) -> int:
        return self.total_home_goals + self.total_away_goals


class PatternArmed(BaseModel):
    kind: Literal["pattern_armed"] = "pattern_armed"
    pattern_name: str
    qualifying_totals: list[int]


class BetPlaced(BaseModel):
    kind: Literal["bet_placed"] = "bet_placed"
    match_id: int
    league_name: str
    home: str
    away: str
    stake: float
    line: float
    odds: float | None = None


class BetFailed(BaseModel):
    kind: Literal["bet_failed"] = "bet_failed"
    match_id: int | None = None
    reason: str


class BetSettled(BaseModel):
    kind: Literal["bet_settled"] = "bet_settled"
    match_id: int
    home: str
    away: str
    won: bool
    first_half_total: int


MatchEvent = (
    MatchDiscovered
    | MatchStarted
    | MatchHalfTime
    | MatchScoreChanged
    | MatchFinished
    | PatternArmed
    | BetPlaced
    | BetFailed
    | BetSettled
)

# kind -> model, used by the bus to parse an incoming domain event without
# the subscriber having to guess which of the nine shapes it received.
MATCH_EVENT_TYPES: dict[str, type[BaseModel]] = {
    "discovered": MatchDiscovered,
    "started": MatchStarted,
    "half_time": MatchHalfTime,
    "score_changed": MatchScoreChanged,
    "finished": MatchFinished,
    "pattern_armed": PatternArmed,
    "bet_placed": BetPlaced,
    "bet_failed": BetFailed,
    "bet_settled": BetSettled,
}
