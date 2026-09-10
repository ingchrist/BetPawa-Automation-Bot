from shared.events import MatchDiscovered
from services.bettor.targeting import TargetTracker


def _discovered(match_id: int, home: str = "A", away: str = "B") -> MatchDiscovered:
    return MatchDiscovered(
        match_id=match_id,
        league_name="FC 25. 3x3. Conference League",
        home=home,
        away=away,
        kickoff_ts=0,
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
