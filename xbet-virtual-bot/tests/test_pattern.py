from services.bettor.pattern import (
    PatternTracker,
    RoundPairStreakTracker,
    SecondHalfLiveConfirmTracker,
)


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


def test_at_or_under_direction_is_the_class_default():
    # PatternTracker()'s own generic defaults (streak_length=3, threshold=6,
    # direction="at_or_under") -- not necessarily Pattern 1's production
    # config, which is set independently via PATTERN_STREAK_LENGTH etc. in
    # shared/config.py.
    tracker = PatternTracker()  # direction defaults to "at_or_under"
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True
    assert tracker.last_streak_totals == [4, 5, 6]


def test_equals_direction_fires_on_two_consecutive_matching_rounds():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True
    assert tracker.last_streak_totals == ["2X", "2X"]


def test_equals_direction_non_matching_round_breaks_the_streak():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("1X") is False  # breaks it -- not a match
    assert tracker.process("2X") is False  # restarts at 1
    assert tracker.process("2X") is True


def test_equals_direction_draw_does_not_qualify():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("X") is False  # a draw is not "2X" -- breaks it
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True


def test_equals_direction_none_total_breaks_the_streak():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process(None) is False
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True


def test_equals_direction_fire_then_skip_one_round_then_restart():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")
    assert tracker.process("2X") is False
    assert tracker.process("2X") is True  # fires

    assert tracker.process("2X") is False  # the skipped bet-target round, regardless of its own result

    assert tracker.process("2X") is False
    assert tracker.process("2X") is True
    assert tracker.last_streak_totals == ["2X", "2X"]


def test_equals_direction_progress_reporting():
    tracker = PatternTracker(threshold="2X", streak_length=2, direction="equals")

    tracker.process("2X")
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (1, "2X", "qualifying")

    tracker.process("1X")
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, "1X", "reset")

    tracker.process("2X")
    tracker.process("2X")
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, "2X", "armed")

    tracker.process("1X")  # the skipped bet-target round
    assert (tracker.streak, tracker.last_total, tracker.last_outcome) == (0, "1X", "skipped")


def test_numeric_directions_unaffected_by_the_equals_addition():
    # PatternTracker() and direction="at_or_over" keep working with plain
    # ints exactly as before -- this is a type-widening, not a behavior
    # change, for the two existing directions.
    tracker = PatternTracker()
    assert tracker.process(4) is False
    assert tracker.process(5) is False
    assert tracker.process(6) is True


def test_pair_fires_live_at_half_time_without_waiting_for_second_half_to_start():
    # Round 1 fully settled with both halves qualifying (>=9). Round 2's
    # 1st half alone is then enough to reach 3 of 4 -- the pattern fires
    # right at round 2's half-time, before its 2nd half is even live.
    tracker = RoundPairStreakTracker()
    assert tracker.on_half_time(match_id=1, first_half_total=9) is False
    assert tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9) is False
    assert tracker.on_half_time(match_id=2, first_half_total=9) is True
    assert tracker.last_pair_values == [9, 9, 9]
    assert tracker.last_qualifying_count == 3


def test_pair_fires_live_mid_second_half_without_waiting_for_finish():
    # Round 1 fully settled with both halves qualifying (2 of 4). Round
    # 2's 1st half does NOT qualify (still 2 of 4), so the pattern has to
    # watch round 2's 2nd half live -- it fires the instant the running
    # total crosses half_threshold, mid-round, without MatchFinished.
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9)
    assert tracker.on_half_time(match_id=2, first_half_total=3) is False  # doesn't qualify, still 2 of 4
    # cumulative goals 3 (1st half) + 5 (2nd half so far) = 8 -- 2nd-half-so-far is 5, not yet 9
    assert tracker.on_score_changed(match_id=2, period_label="2nd half", home_goals=5, away_goals=3) is None
    # cumulative now 3 + 9 = 12 -- 2nd-half-so-far is 9, crosses the threshold mid-round
    assert tracker.on_score_changed(match_id=2, period_label="2nd half", home_goals=7, away_goals=5) is True
    assert tracker.last_pair_values == [9, 9, 3, 9]
    assert tracker.last_qualifying_count == 3


def test_pair_one_round_entirely_under_threshold_prevents_fire():
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=5)
    assert tracker.on_finished(match_id=1, first_half_total=5, second_half_total=5) is False  # 0 of 2 qualify
    tracker.on_half_time(match_id=2, first_half_total=10)
    assert tracker.on_finished(match_id=2, first_half_total=10, second_half_total=12) is False  # values [5,5,10,12] -- only 2 of 4 qualify
    assert tracker.last_pair_values == [5, 5, 10, 12]


def test_pair_fires_when_the_fourth_value_is_needed_to_reach_three():
    # Round 1 (1st=10 qualifies, 2nd=8 does not); Round 2 (1st=10
    # qualifies, 2nd=12 qualifies) -> values [10, 8, 10, 12] -- the 3rd
    # qualifying value only arrives with the pair's 4th and final slot,
    # so this one still can't fire before MatchFinished.
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=10)
    assert tracker.on_finished(match_id=1, first_half_total=10, second_half_total=8) is False
    assert tracker.on_half_time(match_id=2, first_half_total=10) is False  # 2 of 3 so far
    assert tracker.on_finished(match_id=2, first_half_total=10, second_half_total=12) is True
    assert tracker.last_pair_values == [10, 8, 10, 12]


def test_pair_fire_then_skip_one_round_then_restart():
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9)
    assert tracker.on_half_time(match_id=2, first_half_total=9) is True  # fires

    # the round just bet on is skipped, regardless of its own values
    assert tracker.on_half_time(match_id=3, first_half_total=999) is False
    assert tracker.last_outcome == "skipped"
    # its later events are no-ops too -- already resolved by the skip
    assert tracker.on_score_changed(match_id=3, period_label="2nd half", home_goals=999, away_goals=999) is None
    assert tracker.on_finished(match_id=3, first_half_total=999, second_half_total=999) is None

    # next pair after the skip starts counting fresh from 0
    assert tracker.on_half_time(match_id=4, first_half_total=9) is False
    tracker.on_finished(match_id=4, first_half_total=9, second_half_total=9)
    assert tracker.on_half_time(match_id=5, first_half_total=9) is True
    assert tracker.last_pair_values == [9, 9, 9]


def test_pair_none_first_half_total_resets_the_pending_pair():
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9)  # 1 round counted
    assert tracker.on_half_time(match_id=2, first_half_total=None) is False  # resets, does not count as round 2
    assert tracker.last_outcome == "reset"

    # the discarded round doesn't carry over into the next pair -- round
    # 3+4's own 3rd qualifying value is enough to fire right at round 4's
    # half-time, without needing the discarded round 2's values at all
    tracker.on_half_time(match_id=3, first_half_total=9)
    assert tracker.on_finished(match_id=3, first_half_total=9, second_half_total=9) is False
    assert tracker.on_half_time(match_id=4, first_half_total=9) is True


def test_pair_none_second_half_total_resets_the_pending_pair():
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    assert tracker.on_finished(match_id=1, first_half_total=9, second_half_total=None) is False
    assert tracker.last_outcome == "reset"

    tracker.on_half_time(match_id=2, first_half_total=9)
    tracker.on_finished(match_id=2, first_half_total=9, second_half_total=9)
    assert tracker.on_half_time(match_id=3, first_half_total=9) is True


def test_pair_progress_reporting_through_counting_reset_skipped_armed():
    tracker = RoundPairStreakTracker()

    tracker.on_half_time(match_id=1, first_half_total=9)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (1, 1, "counting")
    tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (1, 2, "counting")

    tracker.on_half_time(match_id=2, first_half_total=3)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (2, 2, "counting")
    assert tracker.on_finished(match_id=2, first_half_total=3, second_half_total=3) is False
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (0, 2, "reset")

    tracker.on_half_time(match_id=3, first_half_total=9)
    tracker.on_finished(match_id=3, first_half_total=9, second_half_total=9)
    assert tracker.on_half_time(match_id=4, first_half_total=9) is True
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (0, 3, "armed")

    assert tracker.on_half_time(match_id=5, first_half_total=999) is False  # the skipped bet-target round
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (0, 3, "skipped")

    tracker.on_half_time(match_id=6, first_half_total=9)
    assert (tracker.rounds_in_pair, tracker.last_qualifying_count, tracker.last_outcome) == (1, 1, "counting")


def test_pair_thresholds_are_configurable():
    tracker = RoundPairStreakTracker(half_threshold=5, required_count=4)
    tracker.on_half_time(match_id=1, first_half_total=5)
    assert tracker.on_finished(match_id=1, first_half_total=5, second_half_total=5) is False  # 2 of 2 so far
    tracker.on_half_time(match_id=2, first_half_total=5)
    assert tracker.on_finished(match_id=2, first_half_total=5, second_half_total=4) is False  # values [5,5,5,4] -- only 3 of 4 qualify, required_count=4
    tracker.on_half_time(match_id=3, first_half_total=5)
    assert tracker.on_finished(match_id=3, first_half_total=5, second_half_total=5) is False  # 2 of 2 so far (new pair)
    tracker.on_half_time(match_id=4, first_half_total=5)
    assert tracker.on_finished(match_id=4, first_half_total=5, second_half_total=5) is True  # values [5,5,5,5] -- 4 of 4 qualify


def test_pair_first_half_score_changes_are_not_watched():
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    assert tracker.on_score_changed(match_id=1, period_label="1st half", home_goals=20, away_goals=20) is None


def test_pair_score_change_ignored_before_its_own_half_time_baseline():
    tracker = RoundPairStreakTracker()
    assert tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=9, away_goals=1) is None
    tracker.on_half_time(match_id=1, first_half_total=9)
    assert tracker.on_finished(match_id=1, first_half_total=9, second_half_total=10) is False


def test_pair_resolved_round_ignores_further_calls():
    tracker = RoundPairStreakTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9)
    assert tracker.on_half_time(match_id=1, first_half_total=9) is None
    assert tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9) is None


# --- SecondHalfLiveConfirmTracker (Pattern 5) ---


def test_dead_at_half_time_when_first_half_below_threshold():
    tracker = SecondHalfLiveConfirmTracker()
    assert tracker.on_half_time(match_id=1, first_half_total=8) is False
    # dead -- the 2nd half is never watched, so any later score change is a no-op
    assert tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=10, away_goals=10) is None
    assert tracker.on_finished(match_id=1, first_half_total=8, second_half_total=20) is None


def test_pending_after_qualifying_first_half_no_event_yet():
    tracker = SecondHalfLiveConfirmTracker()
    assert tracker.on_half_time(match_id=1, first_half_total=9) is None  # pending, not resolved


def test_fires_live_mid_second_half_without_waiting_for_finish():
    tracker = SecondHalfLiveConfirmTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    # 2nd half still in progress, cumulative goals 9 (1st half) + 5 (2nd half so far) = 14
    assert tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=8, away_goals=6) is None
    # cumulative now 9 + 9 = 18 -- 2nd-half-so-far is 9, crosses the threshold mid-round
    assert tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=10, away_goals=8) is True
    assert tracker.last_total == 9


def test_first_half_alone_never_fires_even_if_high():
    # Confirms the AND: a huge 1st half does not fire by itself -- the 2nd
    # half must independently also reach the threshold.
    tracker = SecondHalfLiveConfirmTracker()
    assert tracker.on_half_time(match_id=1, first_half_total=20) is None
    assert tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=20, away_goals=3) is None  # 2nd-half-so-far = 3
    assert tracker.on_finished(match_id=1, first_half_total=20, second_half_total=5) is False  # 2nd half never reached 9


def test_finished_is_a_safety_net_when_no_live_score_change_arrives():
    tracker = SecondHalfLiveConfirmTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    assert tracker.on_finished(match_id=1, first_half_total=9, second_half_total=9) is True


def test_resolved_round_ignores_further_calls():
    tracker = SecondHalfLiveConfirmTracker()
    tracker.on_half_time(match_id=1, first_half_total=8)  # resolved dead
    assert tracker.on_half_time(match_id=1, first_half_total=8) is None
    assert tracker.on_finished(match_id=1, first_half_total=8, second_half_total=20) is None


def test_second_half_score_change_ignored_before_its_own_half_time_baseline():
    # The aggregator can emit a 2nd-half ScoreChanged for a match before
    # that match's own MatchHalfTime in the same batch -- see the
    # aggregator's process() ordering. Without a recorded baseline yet,
    # this must not guess (it would overcount by including 1st-half
    # goals) -- MatchHalfTime/MatchFinished resolve it correctly instead.
    tracker = SecondHalfLiveConfirmTracker()
    assert tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=9, away_goals=1) is None
    assert tracker.on_half_time(match_id=1, first_half_total=9) is None
    assert tracker.on_finished(match_id=1, first_half_total=9, second_half_total=10) is True


def test_fire_then_skip_one_round_then_restart():
    tracker = SecondHalfLiveConfirmTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    assert tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=9, away_goals=9) is True  # fires

    # round 2 is the skipped bet-target round, regardless of its own values
    # -- resolved immediately here since its 1st half is itself dead (see
    # test_skip_round_consumed_live_when_its_own_first_half_also_qualifies
    # for the case where round 2's own 1st half also qualifies).
    assert tracker.on_half_time(match_id=2, first_half_total=5) is False
    assert tracker.last_outcome == "skipped"

    # round 3 restarts a fresh evaluation from scratch
    assert tracker.on_half_time(match_id=3, first_half_total=9) is None
    assert tracker.on_score_changed(match_id=3, period_label="2nd half", home_goals=9, away_goals=9) is True


def test_skip_round_consumed_live_when_its_own_first_half_also_qualifies():
    tracker = SecondHalfLiveConfirmTracker()
    tracker.on_half_time(match_id=1, first_half_total=9)
    tracker.on_score_changed(match_id=1, period_label="2nd half", home_goals=9, away_goals=9)  # fires

    # round 2 (skip round) itself qualifies on the 1st half too -- still
    # must not fire, and is consumed the moment it's first resolved
    assert tracker.on_half_time(match_id=2, first_half_total=9) is None  # pending, not yet resolved
    assert tracker.on_score_changed(match_id=2, period_label="2nd half", home_goals=9, away_goals=9) is False
    assert tracker.last_outcome == "skipped"


def test_none_first_half_total_is_dead():
    tracker = SecondHalfLiveConfirmTracker()
    assert tracker.on_half_time(match_id=1, first_half_total=None) is False
    assert tracker.last_outcome == "reset"


def test_first_half_score_changes_are_not_watched():
    # Only the 2nd half is ever watched live -- the 1st half's total is
    # only knowable, and only checked, at MatchHalfTime.
    tracker = SecondHalfLiveConfirmTracker()
    assert tracker.on_score_changed(match_id=1, period_label="1st half", home_goals=9, away_goals=1) is None
    assert tracker.on_half_time(match_id=1, first_half_total=10) is None  # still pending on the 2nd half
