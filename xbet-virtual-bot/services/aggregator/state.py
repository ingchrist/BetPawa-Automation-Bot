"""The state machine: turns a stream of MatchSnapshot "what does it look like
right now" reads into MatchEvent "something changed" transitions.

This is the one place in the whole system that assigns *meaning* — that
upcoming -> live is a kickoff, that live -> finished is worth computing a
result for. Everything upstream (collector) only deals in literal current
state; everything downstream (display, or a future consumer) only deals in
named events. Keeping that judgment in exactly one place is what lets the
display stay a dumb renderer.
"""
from __future__ import annotations

import time

from shared.events import (
    HalfScore,
    MatchDiscovered,
    MatchEvent,
    MatchFinished,
    MatchHalfTime,
    MatchScoreChanged,
    MatchSnapshot,
    MatchStarted,
    MoneylineOdds,
    TotalLine,
)


def _half(snapshot: MatchSnapshot, half_number: int) -> HalfScore | None:
    return next((h for h in snapshot.half_scores if h.half == half_number), None)


def _settle_moneyline(odds: MoneylineOdds | None, home_goals: int, away_goals: int) -> MoneylineOdds | None:
    if odds is None:
        return None
    winner = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
    return odds.model_copy(update={"winner": winner})


def _settle_totals(totals: list[TotalLine], total_goals: int) -> list[TotalLine]:
    return [
        line.model_copy(update={"winner": "over" if total_goals > line.line else "under"})
        for line in totals
    ]


_STATUS_RANK = {"upcoming": 0, "live": 1, "finished": 2}


def _is_stale_reading(previous: MatchSnapshot, snapshot: MatchSnapshot) -> bool:
    """True if `snapshot` looks like an eventually-consistent upstream
    hiccup rather than real match progress, and should be dropped outright —
    not used to update state, not turned into an event, not even allowed to
    become the new baseline for the next comparison.

    Observed live, more than once: this API occasionally serves a stale
    read a poll or two after a fresher one already arrived — the bulk feed
    and a per-match detail call briefly disagreeing, or plain replica lag —
    surfacing as either a match already known live being reported back as
    "upcoming" (which, followed by it going live *again*, duplicated its
    KICK-OFF line and briefly resurrected it as the announced next match),
    or a live match's cumulative goal total dipping before immediately
    recovering. Neither is possible in a self-consistent feed: status only
    ever moves forward (upcoming -> live -> finished), and goals are
    cumulative for the whole match (a finished match's first_half +
    second_half always sums to its total — see _build_finished_event). So
    both are treated as noise rather than real events.
    """
    if _STATUS_RANK[snapshot.status] < _STATUS_RANK[previous.status]:
        return True
    if previous.status == "finished":
        return True  # already fully reported; nothing left to say twice
    if (
        previous.status == "live"
        and snapshot.status == "live"
        and (snapshot.home_goals + snapshot.away_goals) < (previous.home_goals + previous.away_goals)
    ):
        return True
    return False


class MatchStateMachine:
    """Stateful, single-instance, single-process — this is why the aggregator
    is one service rather than several: the transitions below only make
    sense with an unbroken view of "what did this match look like last
    time," which a horizontally-scaled aggregator would have to share some
    other way (fine for a later phase; not needed for phase 1).

    `process()` builds its event list in a fixed order — started, [next
    match announced], score_changed, half_time, finished — so a match that
    jumps two or three states between two polls (a slow poll, a missed
    frame) still reads top to bottom the way it actually happened, never
    e.g. a RESULT block appearing before the HALF-TIME block it belongs
    after.
    """

    def __init__(self) -> None:
        # Entries are kept forever, including finished matches — see the
        # guard at the top of process(). For a single short-format league
        # this is a few hundred small objects a day at most, not worth
        # pruning for phase 1; revisit with a TTL cache if this ever tracks
        # many leagues over long unbroken runs.
        self._last: dict[int, MatchSnapshot] = {}
        # Edge-triggers half-time off "have we announced half 1 for this
        # match yet" rather than off catching the exact "1st half" ->
        # "2nd half" frame — that frame can be skipped outright (a
        # transient detail-fetch failure falls back to the bulk feed's own
        # period label, observed jumping straight past "2nd half" between
        # two polls without ever reporting it in between).
        self._half_time_emitted: set[int] = set()
        # This league queues several matches at once (each with its own
        # countdown), and the collector discovers all of them up front — so
        # without throttling, every poll's first sighting would fan out into
        # 4-5 UPCOMING panels at once. Instead: every currently-upcoming
        # match seen is kept here, and only the soonest-kickoff one of them
        # is ever "announced" (turned into a MatchDiscovered event).
        #
        # This is re-decided on every update rather than locked in once a
        # match is first announced, because kickoff estimates for queued
        # matches genuinely move: these are short-format sim matches whose
        # real duration varies a lot (observed: anywhere from ~10 seconds to
        # 6+ minutes), so an earlier match finishing sooner or later than
        # its estimate reshuffles when everything behind it actually kicks
        # off. A MatchDiscovered only fires when the *identity* of "the
        # soonest one" changes, not on every poll, so in steady state this
        # is quiet — it only re-announces on an actual reordering, or in a
        # quick burst right at startup while several matches are discovered
        # in the same poll before the true soonest is known.
        self._known_upcoming: dict[int, MatchSnapshot] = {}
        self._announced_next_id: int | None = None

    def process(self, snapshot: MatchSnapshot) -> list[MatchEvent]:
        previous = self._last.get(snapshot.match_id)
        if previous is not None and _is_stale_reading(previous, snapshot):
            return []
        self._last[snapshot.match_id] = snapshot
        events: list[MatchEvent] = []

        was_upcoming = previous is None or previous.status == "upcoming"
        if was_upcoming and snapshot.status in ("live", "finished"):
            events.append(self._build_started_event(snapshot))

        announced_event = self._update_upcoming_queue(snapshot)
        if announced_event:
            events.append(announced_event)

        if (
            previous is not None
            and snapshot.status == "live"
            and (snapshot.home_goals, snapshot.away_goals) != (previous.home_goals, previous.away_goals)
        ):
            events.append(self._build_score_changed_event(snapshot))

        half_time_event = self._maybe_half_time(snapshot)
        if half_time_event:
            events.append(half_time_event)

        prev_status = previous.status if previous else None
        if snapshot.status == "finished" and prev_status != "finished":
            events.append(self._build_finished_event(snapshot))

        return events

    def _update_upcoming_queue(self, snap: MatchSnapshot) -> MatchDiscovered | None:
        """Keeps `_known_upcoming` in sync with this snapshot, then
        re-decides which known-upcoming match is soonest and returns a
        MatchDiscovered for it iff that's a *different* match than the one
        already announced. See the note on `_known_upcoming` in __init__
        for why this is re-decided every call rather than fixed at first
        announcement."""
        if snap.status == "upcoming":
            self._known_upcoming[snap.match_id] = snap
        else:
            self._known_upcoming.pop(snap.match_id, None)
            if self._announced_next_id == snap.match_id:
                self._announced_next_id = None

        if not self._known_upcoming:
            return None
        soonest = min(self._known_upcoming.values(), key=lambda s: s.kickoff_ts)
        if soonest.match_id == self._announced_next_id:
            return None
        self._announced_next_id = soonest.match_id
        return self._build_discovered_event(soonest)

    def _build_discovered_event(self, snap: MatchSnapshot) -> MatchDiscovered:
        return MatchDiscovered(
            match_id=snap.match_id,
            league_name=snap.league_name,
            home=snap.home,
            away=snap.away,
            kickoff_ts=snap.kickoff_ts,
            starting_in_label=snap.starting_in_label,
            moneyline=snap.moneyline,
            totals=snap.totals,
        )

    def _build_started_event(self, snap: MatchSnapshot) -> MatchStarted:
        return MatchStarted(match_id=snap.match_id, league_name=snap.league_name, home=snap.home, away=snap.away)

    def _build_score_changed_event(self, snap: MatchSnapshot) -> MatchScoreChanged:
        return MatchScoreChanged(
            match_id=snap.match_id,
            league_name=snap.league_name,
            home=snap.home,
            away=snap.away,
            period_label=snap.period_label,
            clock_seconds=snap.clock_seconds,
            home_goals=snap.home_goals,
            away_goals=snap.away_goals,
            totals=snap.totals,
        )

    def _maybe_half_time(self, snap: MatchSnapshot) -> MatchHalfTime | None:
        """Fires exactly once per match, the first time first-half data is
        available AND the match has moved past the first half (live in the
        2nd, or already finished) — see the note on `_half_time_emitted`
        above for why this isn't a literal frame-to-frame comparison."""
        if snap.match_id in self._half_time_emitted:
            return None
        if snap.status == "upcoming" or snap.period_label == "1st half":
            return None
        first_half = _half(snap, 1)
        if not first_half:
            return None
        self._half_time_emitted.add(snap.match_id)
        return MatchHalfTime(
            match_id=snap.match_id,
            league_name=snap.league_name,
            home=snap.home,
            away=snap.away,
            first_half=first_half,
        )

    def _build_finished_event(self, snap: MatchSnapshot) -> MatchFinished:
        total_goals = snap.home_goals + snap.away_goals
        return MatchFinished(
            match_id=snap.match_id,
            league_name=snap.league_name,
            home=snap.home,
            away=snap.away,
            first_half=_half(snap, 1),
            second_half=_half(snap, 2),
            total_home_goals=snap.home_goals,
            total_away_goals=snap.away_goals,
            moneyline=_settle_moneyline(snap.moneyline, snap.home_goals, snap.away_goals),
            totals=_settle_totals(snap.totals, total_goals),
            finished_at=time.time(),
        )
