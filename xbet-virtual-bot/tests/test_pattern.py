from services.bettor.pattern import PatternTracker


def test_fires_on_three_consecutive_qualifying_rounds():
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True
    assert tracker.last_streak_totals == [4, 5, 6]


def test_high_round_breaks_the_streak():
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(7) is False  # breaks it
    assert tracker.process(4) is False  # restarts at 1
    assert tracker.process(5) is False
    assert tracker.process(6) is True


def test_none_total_breaks_the_streak():
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(None) is False
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True


def test_fire_then_skip_one_round_then_restart():
    tracker = PatternTracker()
    tracker.process(4)
    tracker.process(5)
    assert tracker.process(6) is True  # fires

    # the round just bet on is skipped, regardless of its own total
    assert tracker.process(9) is False

    # next round after the skip starts a fresh streak from 0
    assert tracker.process(3) is False
    assert tracker.process(3) is False
    assert tracker.process(3) is True
    assert tracker.last_streak_totals == [3, 3, 3]


def test_boundary_value_at_threshold_qualifies():
    tracker = PatternTracker(low_threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_boundary_value_above_threshold_does_not_qualify():
    tracker = PatternTracker(low_threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(7) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_streak_length_and_threshold_are_configurable():
    tracker = PatternTracker(low_threshold=5, streak_length=2)
    assert tracker.process(5) is False
    assert tracker.process(5) is True
    assert tracker.last_streak_totals == [5, 5]
