from shared.events import MatchDiscovered
from services.bettor.targeting import TargetTracker, mutual_exclusion_reason


def _discovered(match_id: int, home: str = "A", away: str = "B", kickoff_ts: int = 0) -> MatchDiscovered:
    return MatchDiscovered(
        match_id=match_id,
        league_name="FC 25. 3x3. Conference League",
        home=home,
        away=away,
        kickoff_ts=kickoff_ts,
        starting_in_label="Starting in 1 minute",
    )


def test_arm_returns_already_known_upcoming_match_immediately():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(1))
    target = tracker.arm()
    assert target is not None
    assert target.match_id == 1
    assert 1 in tracker.bet_targets


def test_arm_waits_for_discovery_when_next_match_not_yet_known():
    tracker = TargetTracker()
    target = tracker.arm()
    assert target is None  # nothing known yet — pending

    handed = tracker.on_discovered(_discovered(2))
    assert handed is not None
    assert handed.match_id == 2
    assert 2 in tracker.bet_targets


def test_discovery_without_a_pending_arm_does_not_hand_out_a_target():
    tracker = TargetTracker()
    handed = tracker.on_discovered(_discovered(3))
    assert handed is None
    assert tracker.bet_targets == set()


def test_same_match_is_never_targeted_twice():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(4))
    first = tracker.arm()
    assert first is not None

    # a second, unrelated fire while match 4 is still the latest-known
    # upcoming match must not re-target it
    second = tracker.arm()
    assert second is None


def test_is_stale_true_once_half_time_or_finished_seen():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(5))
    assert tracker.is_stale(5) is False

    tracker.on_started(5)
    assert tracker.is_stale(5) is False

    tracker.on_half_time(5)
    assert tracker.is_stale(5) is True


def test_is_stale_true_once_finished():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(6))
    tracker.on_finished(6)
    assert tracker.is_stale(6) is True


def test_is_stale_false_for_unknown_match():
    tracker = TargetTracker()
    assert tracker.is_stale(999) is False


def test_arm_still_targets_a_match_that_has_already_started():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(7))
    tracker.on_started(7)  # already live, but not yet at half-time
    target = tracker.arm()
    assert target is not None  # started-but-not-stale is still fair game
    assert target.match_id == 7
    assert 7 in tracker.bet_targets


def test_arm_defers_a_target_that_is_already_at_half_time():
    tracker = TargetTracker()
    tracker.on_discovered(_discovered(8))
    tracker.on_started(8)
    tracker.on_half_time(8)  # 1st half already over — genuinely stale
    target = tracker.arm()
    assert target is None  # falls back to waiting for the next discovery


def test_default_stale_statuses_unchanged_half_time_is_stale():
    tracker = TargetTracker()  # default: {"half_time", "finished"}
    tracker.on_discovered(_discovered(10))
    tracker.on_started(10)
    tracker.on_half_time(10)
    assert tracker.is_stale(10) is True


def test_custom_stale_statuses_half_time_is_not_stale():
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(11))
    tracker.on_started(11)
    tracker.on_half_time(11)
    assert tracker.is_stale(11) is False


def test_custom_stale_statuses_finished_is_still_stale():
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(12))
    tracker.on_finished(12)
    assert tracker.is_stale(12) is True


def test_custom_stale_statuses_arm_targets_a_match_already_at_half_time():
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(13))
    tracker.on_started(13)
    tracker.on_half_time(13)  # not stale for this tracker's definition
    target = tracker.arm()
    assert target is not None
    assert target.match_id == 13
    assert 13 in tracker.bet_targets


def test_arm_targets_the_actual_next_round_not_a_later_announcement():
    # Reproduces a live incident: Pattern 4/5 fire well after the real
    # "next round" (20) has already kicked off, by which point the
    # aggregator's soonest-upcoming announcement has already moved on to
    # the round after that (21) -- see _update_upcoming_queue in
    # services/aggregator/state.py, which re-announces the instant the
    # previously-announced match stops being "upcoming". arm() must still
    # resolve to 20 (the real next round, merely live, not stale for this
    # tracker's definition), never to 21.
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(20, "Anderlecht", "Red Bull"))
    tracker.on_started(20)  # real next round already kicked off live
    tracker.on_discovered(_discovered(21, "Lille OSC", "Fenerbahce"))  # aggregator moved on

    target = tracker.arm()
    assert target is not None
    assert target.match_id == 20
    assert 20 in tracker.bet_targets
    assert 21 not in tracker.bet_targets


def test_arm_prefers_the_earlier_kickoff_over_the_earlier_announcement():
    # Reproduces the live incident on 2026-09-20 23:00:59: Braga vs
    # Anderlecht was announced first (a wrong early kickoff estimate),
    # then West Ham vs Lille OSC was announced as the actual sooner
    # match -- but neither had kicked off yet at fire time, so a naive
    # oldest-discovered-first FIFO picked the stale Braga announcement.
    # arm() must pick West Ham (the earlier kickoff_ts), not Braga (the
    # earlier discovery).
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(754743472, "Braga", "Anderlecht", kickoff_ts=2000))
    tracker.on_discovered(_discovered(754741055, "West Ham United", "Lille OSC", kickoff_ts=1000))

    target = tracker.arm()
    assert target is not None
    assert target.match_id == 754741055
    assert 754741055 in tracker.bet_targets
    assert 754743472 not in tracker.bet_targets


def test_arm_still_prefers_an_already_started_match_over_an_earlier_kickoff_estimate():
    # An already-started match is a fact, not an estimate -- it must
    # outrank a still-upcoming match even if that upcoming match's
    # kickoff_ts estimate looks earlier (a stale/overtaken estimate).
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(50, "Started", "Match", kickoff_ts=5000))
    tracker.on_started(50)
    tracker.on_discovered(_discovered(51, "Upcoming", "Match", kickoff_ts=1000))

    target = tracker.arm()
    assert target is not None
    assert target.match_id == 50


def test_arm_skips_a_stale_earlier_announcement_and_falls_through_to_the_next():
    # If the real next round *did* go fully stale (finished) before arm()
    # ran, it's genuinely too late for it -- arm() should fall through to
    # the next still-viable announcement instead of pending forever.
    tracker = TargetTracker(stale_statuses={"finished"})
    tracker.on_discovered(_discovered(30))
    tracker.on_finished(30)  # genuinely too late, not just live
    tracker.on_discovered(_discovered(31))

    target = tracker.arm()
    assert target is not None
    assert target.match_id == 31


def test_pending_arm_resolves_to_the_first_discovery_not_a_later_one():
    tracker = TargetTracker(stale_statuses={"finished"})
    target = tracker.arm()
    assert target is None  # nothing known yet -- pending

    # Pending resolves synchronously on the very next discovery -- it
    # must not sit waiting for a second one to pile up behind it.
    handed = tracker.on_discovered(_discovered(40))
    assert handed is not None
    assert handed.match_id == 40
    assert 40 in tracker.bet_targets

    tracker.on_started(40)
    # A further-out match discovered afterward must not retarget or
    # otherwise disturb the already-resolved match 40.
    further = tracker.on_discovered(_discovered(41))
    assert further is None
    assert 41 not in tracker.bet_targets


def test_mutual_exclusion_reason_none_when_match_not_claimed_by_other():
    assert mutual_exclusion_reason(1, "pattern2", set()) is None
    assert mutual_exclusion_reason(1, "pattern2", {2, 3}) is None


def test_mutual_exclusion_reason_set_when_match_already_claimed_by_other():
    reason = mutual_exclusion_reason(1, "pattern2", {1, 2})
    assert reason == "mutual exclusion: match 1 already targeted by pattern2"


def test_mutual_exclusion_reason_composes_across_two_other_target_sets():
    # Simulates place()'s fan-out: checking against pattern 2's targets, then
    # pattern 3's, in sequence, short-circuiting on the first conflict.
    match_id = 123
    other_targets_2 = {match_id}  # pattern 2 has claimed this match
    other_targets_3 = set()  # pattern 3 has not
    other_patterns = [(other_targets_2, "pattern_2"), (other_targets_3, "pattern_3")]

    conflict = None
    for targets, name in other_patterns:
        conflict = mutual_exclusion_reason(match_id, name, targets)
        if conflict is not None:
            break

    assert conflict is not None
    assert "pattern_2" in conflict


def test_mutual_exclusion_reason_three_way_both_others_block():
    # Pattern A claims a match; both pattern B and pattern C separately
    # detect the conflict against A's target set when they each try to
    # target the same match (mirroring the three-pattern scenario the
    # design spec's Testing section calls for).
    match_id = 456
    targets_a = {match_id}

    reason_for_b = mutual_exclusion_reason(match_id, "pattern_a", targets_a)
    reason_for_c = mutual_exclusion_reason(match_id, "pattern_a", targets_a)

    assert reason_for_b is not None
    assert reason_for_c is not None
    assert "pattern_a" in reason_for_b
    assert "pattern_a" in reason_for_c
