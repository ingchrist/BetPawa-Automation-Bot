"""Turns MatchEvents into the terminal blocks the operator actually reads.

Every render_* function *prints* (via a shared rich Console) rather than
returning text — each call appends one block to normal terminal scrollback,
same as the existing BetPawa bot's NEXT/RESULT blocks. Nothing here ever
clears the screen or redraws in place: that's a deliberate choice, not an
oversight — the whole point is that scrolling up shows you every past
event, live score change and result exactly in the order they happened.

Layout follows a reference design (a compact two-box scoreboard card: league
+ score + clock on the left, the Total O/U ladder on the right) rather than
the tall single-column table this started with — that original layout
burned too much vertical space, especially once several matches' blocks
piled up in scrollback. Two rules came out of that redesign, both intentional
product decisions, not omissions:

  - Odds only ever appear next to a score. UPCOMING shows just the kickoff
    countdown — no market moves while nothing's happening yet to move it
    against — and the Total ladder only starts appearing once a match is
    actually live (`render_live_score`), continuing through RESULT.
  - Only one upcoming match is ever shown, the very next one to kick off —
    this file just renders whatever MatchDiscovered it's given; the
    one-at-a-time selection itself happens in
    `services/aggregator/state.py` (`_update_upcoming_queue`).

Color language:
  yellow  — upcoming (the "before")
  green   — live / in progress
  cyan    — finished (the "after")
  dim     — losing side of a settled odds line
  bold    — the number that matters most in a line (a score, a price)
"""
from __future__ import annotations

from datetime import datetime

from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shared.events import (
    MatchDiscovered,
    MatchFinished,
    MatchHalfTime,
    MatchScoreChanged,
    MatchStarted,
    MoneylineOdds,
    TotalLine,
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


def _totals_grid(totals: list[TotalLine]) -> Table | None:
    """The "O 15.5  1.195   U 15.5  4.08" ladder, one line per threshold,
    Over and Under side by side rather than stacked — this is the layout
    the reference design uses for the "Total" box."""
    if not totals:
        return None
    grid = Table.grid(padding=(0, 2, 0, 0))
    grid.add_column()
    grid.add_column()
    for line in totals:
        over = Text(f"O {line.line:g}  {line.over:g}")
        under = Text(f"U {line.line:g}  {line.under:g}")
        if line.winner == "over":
            over.stylize("bold green")
            over.append(" ✓", style="bold green")
            under.stylize("dim")
        elif line.winner == "under":
            under.stylize("bold green")
            under.append(" ✓", style="bold green")
            over.stylize("dim")
        grid.add_row(over, under)
    return grid


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
    a self-contained snapshot (score, period, clock, current Total ladder)
    rather than a bare "+1" line, so scrolling back through a match's goals
    reads as a series of complete moments, not a diff you have to replay in
    your head."""
    score_card = Panel(
        Group(
            Text(event.league_name, style="bold"),
            Text(""),
            _score_line(event.home, event.home_goals, event.away_goals, event.away),
            Text(f"{event.period_label} · {_match_clock(event.clock_seconds)}", style="dim"),
        ),
        border_style="green",
    )
    totals = _totals_grid(event.totals)
    if totals is None:
        console.print(score_card)
        return
    total_card = Panel(Group(Text("Total", style="bold"), totals), border_style="blue")
    console.print(Columns([score_card, total_card], equal=False, expand=False))


def render_half_time(event: MatchHalfTime) -> None:
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
    halves = Table.grid(padding=(0, 2, 0, 0))
    halves.add_column()
    halves.add_column(justify="right")
    halves.add_column(justify="right")
    halves.add_column(justify="right")
    halves.add_row("", "1st half", "2nd half", "Total")
    halves.add_row(
        event.home,
        str(h1.home_goals) if h1 else "–",
        str(h2.home_goals) if h2 else "–",
        Text(str(event.total_home_goals), style="bold"),
    )
    halves.add_row(
        event.away,
        str(h1.away_goals) if h1 else "–",
        str(h2.away_goals) if h2 else "–",
        Text(str(event.total_away_goals), style="bold"),
    )

    parts = [halves]
    moneyline = _moneyline_row(event.moneyline)
    if moneyline:
        parts.append(moneyline)
    totals = _totals_grid(event.totals)
    if totals:
        parts.append(Text("Total", style="bold"))
        parts.append(totals)

    return Panel(
        Group(*parts),
        title=f"[{style}]{label}[/{style}]  [bold]{event.league_name}[/bold]  ·  {event.home} vs {event.away}",
        subtitle=f"{event.total_goals} goal{'s' if event.total_goals != 1 else ''}",
        subtitle_align="right",
        border_style=style,
        title_align="left",
    )


def render_finished(event: MatchFinished, *, earlier: bool = False) -> None:
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


def render_legend() -> None:
    console.print(
        "[dim]Legend: [yellow]UPCOMING[/yellow] = the next match to kick off (no odds yet) · "
        "[green]●[/green] live update, with the current Total O/U ladder · "
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
