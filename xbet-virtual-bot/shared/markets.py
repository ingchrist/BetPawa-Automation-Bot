"""Pure, shared market-grading helpers — no I/O, no state, reusable by any
service that needs to know how a market actually settles from raw goal
counts. Currently just Double Chance, moved here from
services/display/render.py so services/bettor/main.py (Pattern 3) can
derive the same "1X"/"2X"/"X" result from a match's 1st-half goals
without a second, potentially-drifting copy of the same rule.
"""
from __future__ import annotations


def double_chance_winner(home_goals: int, away_goals: int) -> str:
    """Which Double Chance selection actually settles as the winner for a
    half (or the whole match), given the final score for that scope —
    "1X" (home win or draw), "2X" (away win or draw), or "X" for an
    outright draw (not itself a Double Chance selection, but the
    plain-language answer when both 1X and 2X would settle as winners).
    Derived straight from goal counts already on hand — this is exactly
    how the market grades, so there's nothing to fetch."""
    if home_goals > away_goals:
        return "1X"
    if away_goals > home_goals:
        return "2X"
    return "X"
