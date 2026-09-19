from pathlib import Path

from services.aggregator.history import ResultsHistory
from shared.events import MatchFinished


def _finished(match_id: int) -> MatchFinished:
    return MatchFinished(
        match_id=match_id,
        league_name="FC 25. 3x3. Conference League",
        home="A",
        away="B",
        total_home_goals=1,
        total_away_goals=2,
        finished_at=0.0,
    )


def test_load_recent_skips_a_corrupted_trailing_line(tmp_path: Path):
    # Reproduces the on-disk corruption seen in data/results.jsonl: a
    # partially-written line left with a run of leading null bytes ahead of
    # an otherwise complete, valid record (consistent with an interrupted
    # disk write rather than an application bug).
    path = tmp_path / "results.jsonl"
    good = _finished(1).model_dump_json()
    corrupted = "\x00" * 32 + _finished(2).model_dump_json()
    path.write_text(good + "\n" + corrupted + "\n")

    history = ResultsHistory(path)
    recent = history.load_recent(5)

    assert [event.match_id for event in recent] == [1]


def test_load_recent_returns_all_valid_lines_when_nothing_is_corrupted(tmp_path: Path):
    path = tmp_path / "results.jsonl"
    lines = [_finished(i).model_dump_json() for i in range(1, 4)]
    path.write_text("\n".join(lines) + "\n")

    history = ResultsHistory(path)
    recent = history.load_recent(5)

    assert [event.match_id for event in recent] == [1, 2, 3]
