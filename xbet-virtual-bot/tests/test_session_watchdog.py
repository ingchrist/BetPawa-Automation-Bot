"""tests/test_session_watchdog.py"""
import asyncio
import logging

import pytest

from services.bettor.session_watchdog import LoginResult, watchdog_loop


async def _run_briefly(coro, wall_clock_seconds: float = 0.05) -> None:
    """Runs an infinite watchdog_loop coroutine for a short bounded wall-
    clock window, then cancels it. The loop's own `sleep` is faked to
    return instantly, so this window is enough for many iterations."""
    task = asyncio.create_task(coro)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=wall_clock_seconds)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _instant_sleep():
    async def sleep(_seconds: float) -> None:
        return None
    return sleep


def _static_clock(value: float = 0.0):
    return lambda: value


def _advancing_clock(step: float):
    state = {"t": 0.0}

    def now() -> float:
        state["t"] += step
        return state["t"]

    return now


@pytest.mark.asyncio
async def test_dead_session_triggers_exactly_one_login_attempt_within_cooldown():
    login_calls = []

    async def fake_is_alive(cdp_url):
        return False

    async def fake_login(cdp_url, phone, password, timeout_seconds):
        login_calls.append((cdp_url, phone, password))
        return LoginResult(success=True)

    await _run_briefly(
        watchdog_loop(
            "http://cdp", "673112163", "secret",
            check_interval_seconds=1, login_cooldown_seconds=300, login_timeout_seconds=15,
            log=logging.getLogger("test"),
            is_alive=fake_is_alive, do_login=fake_login,
            sleep=_instant_sleep(), now=_static_clock(0.0),
        )
    )

    assert login_calls == [("http://cdp", "673112163", "secret")]


@pytest.mark.asyncio
async def test_alive_session_never_attempts_login():
    login_calls = []

    async def fake_is_alive(cdp_url):
        return True

    async def fake_login(cdp_url, phone, password, timeout_seconds):
        login_calls.append((cdp_url, phone, password))
        return LoginResult(success=True)

    await _run_briefly(
        watchdog_loop(
            "http://cdp", "673112163", "secret",
            check_interval_seconds=1, login_cooldown_seconds=300, login_timeout_seconds=15,
            log=logging.getLogger("test"),
            is_alive=fake_is_alive, do_login=fake_login,
            sleep=_instant_sleep(), now=_static_clock(0.0),
        )
    )

    assert login_calls == []


@pytest.mark.asyncio
async def test_persistently_dead_session_respects_cooldown_then_retries():
    login_calls = []

    async def fake_is_alive(cdp_url):
        return False  # never recovers on its own

    async def fake_login(cdp_url, phone, password, timeout_seconds):
        login_calls.append((cdp_url, phone, password))
        return LoginResult(success=False, reason="wrong password")

    # Advances 100 simulated seconds per `now()` call. A 300s cooldown
    # needs ~3-4 calls to elapse before a second attempt is allowed --
    # comfortably within the number of loop iterations a short wall-clock
    # window produces with an instant fake sleep.
    await _run_briefly(
        watchdog_loop(
            "http://cdp", "673112163", "secret",
            check_interval_seconds=1, login_cooldown_seconds=300, login_timeout_seconds=15,
            log=logging.getLogger("test"),
            is_alive=fake_is_alive, do_login=fake_login,
            sleep=_instant_sleep(), now=_advancing_clock(step=100.0),
        ),
        wall_clock_seconds=0.1,
    )

    assert len(login_calls) >= 2, "expected the cooldown to expire and a second attempt to fire"


@pytest.mark.asyncio
async def test_successful_login_does_not_repeat_once_session_reports_alive():
    login_calls = []
    alive_responses = iter([False, True, True, True, True, True, True, True, True, True])

    async def fake_is_alive(cdp_url):
        try:
            return next(alive_responses)
        except StopIteration:
            return True  # stays alive for any further calls the loop makes

    async def fake_login(cdp_url, phone, password, timeout_seconds):
        login_calls.append((cdp_url, phone, password))
        return LoginResult(success=True)

    # Cooldown is deliberately tiny and the clock advances fast, so if the
    # implementation were (incorrectly) driven by cooldown alone rather
    # than by re-checking is_alive first, it would attempt a second login
    # well within this test's window.
    await _run_briefly(
        watchdog_loop(
            "http://cdp", "673112163", "secret",
            check_interval_seconds=1, login_cooldown_seconds=1, login_timeout_seconds=15,
            log=logging.getLogger("test"),
            is_alive=fake_is_alive, do_login=fake_login,
            sleep=_instant_sleep(), now=_advancing_clock(step=100.0),
        ),
        wall_clock_seconds=0.1,
    )

    assert login_calls == [("http://cdp", "673112163", "secret")], (
        "a session that reports alive again after a successful login must not trigger another attempt"
    )


@pytest.mark.asyncio
async def test_missing_credentials_returns_immediately_without_looping():
    login_calls = []

    async def fake_is_alive(cdp_url):
        return False

    async def fake_login(cdp_url, phone, password, timeout_seconds):
        login_calls.append((cdp_url, phone, password))
        return LoginResult(success=True)

    # No timeout/cancellation needed here -- a correct implementation
    # returns immediately instead of looping forever.
    await asyncio.wait_for(
        watchdog_loop(
            "http://cdp", "", "",
            check_interval_seconds=1, login_cooldown_seconds=300, login_timeout_seconds=15,
            log=logging.getLogger("test"),
            is_alive=fake_is_alive, do_login=fake_login,
            sleep=_instant_sleep(), now=_static_clock(0.0),
        ),
        timeout=1.0,
    )

    assert login_calls == []
