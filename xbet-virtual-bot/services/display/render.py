"""Turns MatchEvents into the terminal blocks the operator actually reads.

Every render_* function *prints* (via a shared rich Console) rather than
returning text — each call appends one block to normal terminal scrollback,
same as the existing BetPawa bot's NEXT/RESULT blocks. Nothing here ever
clears the screen or redraws in place: that's a deliberate choice, not an
oversight — the whole point is that scrolling up shows you every past
event, live score change and result exactly in the order they happened.

Layout follows a reference design: a "Result" grid (home/away rows, goals
per half, plus the *combined* both-teams goal total for each half and for
the match overall) with a plain elapsed-time line underneath, rather than
the odds-ladder ("Total" O/U price) box this used to sit next to. A raw
goal count answers an Over/Under question on its own — the ladder was
printed on every single goal (`render_live_score` fires that often) and
duplicating a mostly-unchanging price table that many times in scrollback
was noise, not signal. Two rules carried over from the layout before this
one, both intentional product decisions, not omissions:

  - UPCOMING shows just the kickoff countdown — no score/result data to
    show yet.
  - Only one upcoming match is ever shown, the very next one to kick off —
    this file just renders whatever MatchDiscovered it's given; the
    one-at-a-time selection itself happens in
    `services/aggregator/state.py` (`_update_upcoming_queue`).

A live match's 1st/2nd-half split isn't in MatchScoreChanged (it only
carries the running total) — `_half_time_cache` below holds the 1st-half
score last reported by MatchHalfTime, per match, purely so the live grid
can show a real split instead of dumping everything into "2nd half" until
the match actually finishes.

Color language:
  yellow  — upcoming (the "before")
  green   — live / in progress
  cyan    — finished (the "after")
  dim     — losing side of a settled odds line
  bold    — the number that matters most in a line (a score, a total)
"""
from __future__ import annotations

import time
from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shared.events import (
    BetFailed,
    BetPlaced,
    BetSettled,
    HalfScore,
    MatchDiscovered,
    MatchFinished,
    MatchHalfTime,
    MatchScoreChanged,
    MatchStarted,
    MoneylineOdds,
    PatternArmed,
)

console = Console(highlight=False)


def _clock(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _datetime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _match_clock(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _score_line(home: str, home_goals: int, away_goals: int, away: str) -> Text:
    text = Text()
    text.append(f"{home}  ", style="bold")
    text.append(f"{home_goals} : {away_goals}", style="bold cyan")
    text.append(f"  {away}", style="bold")
    return text


def _moneyline_row(odds: MoneylineOdds | None) -> Text | None:
    """A single "W1 1.97 ✓  X 7.60  W2 2.30" line — kept to one row since,
    unlike Total, there's only ever the one market."""
    if odds is None:
        return None
    text = Text()

    def cell(label: str, price: float, side: str, sep: str) -> None:
        chunk = f"{label} {price:.2f}"
        if odds.winner is None:
            text.append(chunk)
        elif odds.winner == side:
            text.append(chunk, style="bold green")
            text.append(" ✓", style="bold green")
        else:
            text.append(chunk, style="dim")
        text.append(sep)

    cell("W1", odds.home, "home", "  ")
    cell("X", odds.draw, "draw", "  ")
    cell("W2", odds.away, "away", "")
    return text


# The 1st-half score last seen via render_half_time(), keyed by match_id —
# see the module docstring for why the live grid needs this. Popped once a
# match finishes (render_finished()) so this doesn't grow unbounded.
_half_time_cache: dict[int, HalfScore] = {}


def _result_table(
    home: str,
    away: str,
    h1_home: int | None,
    h1_away: int | None,
    h2_home: int | None,
    h2_away: int | None,
    total_home: int,
    total_away: int,
) -> Table:
    """The "Result" grid from the reference design: goals per half for each
    team, plus the *combined* (both-teams) goal count for each half and for
    the match overall — that combined number is what actually answers an
    Over/Under question, in place of the price ladder this replaced. A half
    stays "–" until that half's score is actually known, rather than
    guessed at from a still-in-progress running total."""
    table = Table(header_style="bold")
    table.add_column("Result")
    table.add_column("1st half", justify="right")
    table.add_column("total for\n1st half", justify="center")
    table.add_column("2nd half", justify="right")
    table.add_column("total for\n2nd half", justify="center")
    table.add_column("final total", justify="center")

    def cell(value: int | None) -> str:
        return "–" if value is None else str(value)

    h1_total = None if h1_home is None or h1_away is None else h1_home + h1_away
    h2_total = None if h2_home is None or h2_away is None else h2_home + h2_away

    table.add_row(
        home,
        cell(h1_home),
        Text(cell(h1_total), style="bold") if h1_total is not None else "",
        cell(h2_home),
        "",
        "",
    )
    table.add_row(
        away,
        cell(h1_away),
        "",
        cell(h2_away),
        Text(cell(h2_total), style="bold") if h2_total is not None else "",
        Text(str(total_home + total_away), style="bold"),
    )
    return table


def render_discovered(event: MatchDiscovered) -> None:
    """UPCOMING — deliberately just the kickoff line. No odds: there's
    nothing live yet for a market move to mean anything against, and this
    is the one block guaranteed to show for every match, so keeping it to
    a single line is what actually saves the vertical space the old
    all-panels-at-once layout was losing."""
    console.print(
        Panel(
            f"Kickoff: {event.starting_in_label or _clock(event.kickoff_ts)}",
            title=f"[yellow]UPCOMING[/yellow]  [bold]{event.league_name}[/bold]  ·  {event.home} vs {event.away}",
            border_style="yellow",
            title_align="left",
        )
    )


def render_started(event: MatchStarted) -> None:
    console.print(f"[green]● KICK-OFF[/green]  {event.league_name}  ·  {event.home} vs {event.away}")


def render_live_score(event: MatchScoreChanged) -> None:
    """The recurring live scoreboard card — printed on every goal, each one
    a self-contained snapshot (score, half-by-half Result grid, elapsed
    time) rather than a bare "+1" line, so scrolling back through a match's
    goals reads as a series of complete moments, not a diff you have to
    replay in your head."""
    half1 = _half_time_cache.get(event.match_id)
    if "1st" in event.period_label:
        h1_home, h1_away = event.home_goals, event.away_goals
        h2_home = h2_away = None
    elif half1 is not None:
        h1_home, h1_away = half1.home_goals, half1.away_goals
        h2_home, h2_away = event.home_goals - half1.home_goals, event.away_goals - half1.away_goals
    else:
        # 2nd half already under way but no half-time score was ever cached
        # for it (e.g. display restarted mid-match) — show the running
        # total as "2nd half" rather than guess at a 1st-half split we
        # don't actually have.
        h1_home = h1_away = None
        h2_home, h2_away = event.home_goals, event.away_goals

    console.print(
        Panel(
            Group(
                _score_line(event.home, event.home_goals, event.away_goals, event.away),
                Text(""),
                _result_table(event.home, event.away, h1_home, h1_away, h2_home, h2_away, event.home_goals, event.away_goals),
                Text(
                    f"time elapse   {event.period_label} · {_match_clock(event.clock_seconds)}",
                    style="dim",
                    justify="center",
                ),
            ),
            title=f"[green]● LIVE[/green]  [bold]{event.league_name}[/bold]  ·  {event.home} vs {event.away}",
            border_style="green",
            title_align="left",
        )
    )


def render_half_time(event: MatchHalfTime) -> None:
    _half_time_cache[event.match_id] = event.first_half
    console.print(
        f"[green]● HALF-TIME[/green]  {event.league_name}  ·  "
        f"{event.home} {event.first_half.home_goals} - {event.first_half.away_goals} {event.away}"
    )


def build_finished_panel(event: MatchFinished, *, earlier: bool = False) -> Panel:
    """The RESULT block. Split out from render_finished() so the exact same
    renderable can also be appended to the plain-text prediction log (see
    log_finished() and services/display/main.py) without rebuilding it by
    hand or duplicating the table layout in two places."""
    label = "EARLIER RESULT" if earlier else "RESULT"
    style = "dim cyan" if earlier else "cyan"

    h1, h2 = event.first_half, event.second_half
    result = _result_table(
        event.home,
        event.away,
        h1.home_goals if h1 else None,
        h1.away_goals if h1 else None,
        h2.home_goals if h2 else None,
        h2.away_goals if h2 else None,
        event.total_home_goals,
        event.total_away_goals,
    )

    parts = [result]
    moneyline = _moneyline_row(event.moneyline)
    if moneyline:
        parts.append(moneyline)

    return Panel(
        Group(*parts),
        title=f"[{style}]{label}[/{style}]  [bold]{event.league_name}[/bold]  ·  {event.home} vs {event.away}",
        subtitle=f"{event.total_goals} goal{'s' if event.total_goals != 1 else ''}",
        subtitle_align="right",
        border_style=style,
        title_align="left",
    )


def render_finished(event: MatchFinished, *, earlier: bool = False) -> None:
    _half_time_cache.pop(event.match_id, None)
    console.print(build_finished_panel(event, earlier=earlier))


def log_finished(event: MatchFinished, target: Console) -> None:
    """Appends the same RESULT block render_finished() prints to the
    terminal onto `target` instead — used to keep a standing, human-readable
    record of every finished round in data/result.log (plain text, no ANSI:
    `target` is expected to be a no-color Console) independent of both the
    terminal's scrollback and the machine-readable data/results.jsonl."""
    target.print(_datetime(event.finished_at))
    target.print(build_finished_panel(event))
    target.print()


def render_pattern_armed(event: PatternArmed) -> None:
    totals = ", ".join(str(t) for t in event.qualifying_totals)
    console.print(
        Panel(
            f"3 consecutive rounds at or under the threshold: {totals}\n"
            "Betting on the next round's 1st-half Over line.",
            title="[magenta]◈ PATTERN FIRED[/magenta]  1st Half Over — streak",
            border_style="magenta",
            title_align="left",
        )
    )


def render_bet_placed(event: BetPlaced) -> None:
    odds_str = f" @ {event.odds:.2f}" if event.odds is not None else ""
    console.print(
        f"[green]✓ BET PLACED[/green]  {event.stake:g} on {event.home} vs {event.away} "
        f"— Total. 1st half Over {event.line:g}{odds_str}"
    )


def render_bet_failed(event: BetFailed) -> None:
    where = f" (match {event.match_id})" if event.match_id is not None else ""
    console.print(f"[red]✗ BET FAILED[/red]{where}  {event.reason}")


def render_bet_settled(event: BetSettled) -> None:
    label = "[bold green]WON[/bold green]" if event.won else "[bold red]LOST[/bold red]"
    console.print(
        f"[cyan]● SETTLED[/cyan]  {event.home} vs {event.away} "
        f"— 1st half total {event.first_half_total} — {label}"
    )


BettorEvent = PatternArmed | BetPlaced | BetFailed | BetSettled


def log_bet_event(event: BettorEvent, target: Console) -> None:
    """Plain-text twin of the four render_* functions above, appended to
    data/bets.log — same convention as log_finished()/data/result.log."""
    target.print(_datetime(time.time()))
    if isinstance(event, PatternArmed):
        target.print(f"PATTERN ARMED — streak {event.qualifying_totals}")
    elif isinstance(event, BetPlaced):
        odds_str = f" @ {event.odds:.2f}" if event.odds is not None else ""
        target.print(
            f"BET PLACED — {event.stake:g} on {event.home} vs {event.away} "
            f"(match {event.match_id}) Total. 1st half Over {event.line:g}{odds_str}"
        )
    elif isinstance(event, BetFailed):
        target.print(f"BET FAILED — match {event.match_id}: {event.reason}")
    elif isinstance(event, BetSettled):
        outcome = "WON" if event.won else "LOST"
        target.print(
            f"SETTLED — {event.home} vs {event.away}: {outcome} "
            f"(1st half total {event.first_half_total})"
        )
    target.print()


def render_legend() -> None:
    console.print(
        "[dim]Legend: [yellow]UPCOMING[/yellow] = the next match to kick off (no score yet) · "
        "[green]●[/green] live update, with the Result grid and elapsed time so far · "
        "[cyan]RESULT[/cyan] = final score. "
        "✓ marks the settled winning side; scroll up for earlier results.[/dim]"
    )
    console.print()


def render_backfill_header(count: int) -> None:
    if count == 0:
        return
    console.print(f"[dim]— replaying {count} earlier result(s) from history —[/dim]")


def render_live_header() -> None:
    console.print("[dim]— now watching live —[/dim]")
    console.print()
