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
    tracker = PatternTracker(threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_boundary_value_above_threshold_does_not_qualify():
    tracker = PatternTracker(threshold=6, streak_length=3)
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(7) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is False
    assert tracker.process(6) is True


def test_streak_length_and_threshold_are_configurable():
    tracker = PatternTracker(threshold=5, streak_length=2)
    assert tracker.process(5) is False
    assert tracker.process(5) is True
    assert tracker.last_streak_totals == [5, 5]


def test_progress_reporting_across_qualify_reset_skip():
    tracker = PatternTracker()

    tracker.process(4)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (1, 4, "qualifying")

    tracker.process(9)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 9, "reset")

    tracker.process(4)
    tracker.process(5)
    tracker.process(6)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 6, "armed")

    tracker.process(9)  # the skipped bet-target round, regardless of its own total
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 9, "skipped")

    tracker.process(3)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (1, 3, "qualifying")


def test_progress_reports_reset_on_none_total():
    tracker = PatternTracker()
    tracker.process(4)
    tracker.process(None)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, None, "reset")


def test_at_or_over_direction_fires_on_three_consecutive_high_rounds():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    assert tracker.process(9) is False
    assert tracker.process(10) is False
    assert tracker.process(8) is True
    assert tracker.last_streak_totals == [9, 10, 8]


def test_at_or_over_direction_low_round_breaks_the_streak():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    assert tracker.process(9) is False
    assert tracker.process(7) is False  # breaks it -- below threshold
    assert tracker.process(9) is False  # restarts at 1
    assert tracker.process(9) is False
    assert tracker.process(9) is True


def test_at_or_over_direction_none_total_breaks_the_streak():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    assert tracker.process(9) is False
    assert tracker.process(None) is False
    assert tracker.process(9) is False
    assert tracker.process(9) is False
    assert tracker.process(9) is True


def test_at_or_over_direction_fire_then_skip_one_round_then_restart():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")
    tracker.process(9)
    tracker.process(10)
    assert tracker.process(8) is True  # fires

    assert tracker.process(2) is False  # the skipped bet-target round

    assert tracker.process(9) is False
    assert tracker.process(9) is False
    assert tracker.process(9) is True
    assert tracker.last_streak_totals == [9, 9, 9]


def test_at_or_over_direction_progress_reporting():
    tracker = PatternTracker(threshold=8, streak_length=3, direction="at_or_over")

    tracker.process(9)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (1, 9, "qualifying")

    tracker.process(3)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 3, "reset")

    tracker.process(9)
    tracker.process(10)
    tracker.process(8)
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 8, "armed")

    tracker.process(2)  # the skipped bet-target round
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, 2, "skipped")


def test_at_or_under_direction_default_is_pattern_1s_exact_existing_behavior():
    tracker = PatternTracker()  # direction defaults to "at_or_under"
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True
    assert tracker.last_streak_totals == [4, 5, 6]
