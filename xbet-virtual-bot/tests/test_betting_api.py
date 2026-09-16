import asyncio
import json

import httpx

from services.bettor.betting_api import AuthTokens, BetExecutor

FAKE_AUTH = AuthTokens(access_token="jwt-abc", hd_token="hd-xyz", user_id=298028641)


async def _fake_auth_ok() -> AuthTokens:
    return FAKE_AUTH


async def _fake_auth_fails() -> AuthTokens:
    raise RuntimeError("no access_token cookie")


def _game_zip_response(events: list[dict]) -> dict:
    return {"Value": {"E": events}}


def _matching_event(coef: float = 1.408, line: float = 6.5, t: int = 9) -> dict:
    return {"T": t, "P": line, "G": 17, "C": coef}


def _executor(handler, auth_reader=_fake_auth_ok) -> BetExecutor:
    transport = httpx.MockTransport(handler)
    return BetExecutor(
        api_base="https://1xbet.cm/service-api",
        cdp_url="http://127.0.0.1:9222",
        timeout_seconds=5.0,
        auth_reader=auth_reader,
        transport=transport,
    )


def test_successful_bet_returns_success_with_odds():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event()]))
        assert request.url.path.endswith("/MakeBetWeb")
        body = json.loads(request.content)
        assert body["UserId"] == 298028641
        assert body["Summ"] == 90
        assert body["Events"][0]["GameId"] == 751444118  # match_id + FIRST_HALF_ID_OFFSET
        assert body["Events"][0]["Coef"] == 1.408
        assert body["Events"][0]["Param"] == 6.5
        assert request.headers["x-auth"] == "Bearer jwt-abc"
        assert request.headers["x-hd"] == "hd-xyz"
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(match_id=751444117, home="A", away="B", stake=90, line=6.5)
    )
    asyncio.run(executor.aclose())

    assert result.success is True
    assert result.odds == 1.408
    assert result.reason is None


def test_odds_lookup_and_bet_both_target_the_first_half_sub_id():
    """The raw match_id is the whole-match ("Main game") id, not the
    1st-half market -- confirmed 2026-09-10 by placing/inspecting real
    bets against match_id, match_id+1, and match_id+2 side by side.
    match_id+1 is the unambiguous 1st-half sub-game id."""
    seen_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            seen_ids.append(int(request.url.params["id"]))
            return httpx.Response(200, json=_game_zip_response([_matching_event()]))
        body = json.loads(request.content)
        seen_ids.append(body["Events"][0]["GameId"])
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    asyncio.run(executor.place_bet(match_id=751444117, home="A", away="B", stake=90, line=6.5))
    asyncio.run(executor.aclose())

    assert seen_ids == [751444118, 751444118]


def test_auth_failure_does_not_hit_the_network():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    executor = _executor(handler, auth_reader=_fake_auth_fails)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert "auth read failed" in result.reason
    assert calls == []


def test_no_matching_market_is_treated_as_stale_fire_guard():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/GetGameZip")
        return httpx.Response(200, json=_game_zip_response([]))

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90, line=6.5))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert result.reason == "market not open (stale-fire guard)"


def test_wrong_line_is_not_matched():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_game_zip_response([_matching_event(line=7.5)]))

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90, line=6.5))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert result.reason == "market not open (stale-fire guard)"


def test_odds_lookup_http_error_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert "odds lookup failed" in result.reason


def test_make_bet_rejected_by_server_is_reported_as_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event()]))
        return httpx.Response(
            200,
            json={"Value": None, "Success": False, "Error": "Insufficient funds", "ErrorCode": 42},
        )

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert result.reason == "Insufficient funds"
    assert result.odds == 1.408


def test_make_bet_http_error_reports_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event()]))
        return httpx.Response(400, json={"Error": "invalid market", "ErrorCode": 7})

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert "invalid market" in result.reason


def test_make_bet_network_error_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event()]))
        raise httpx.ConnectError("connection refused")

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert "request failed" in result.reason


def test_period_2_over_false_targets_second_half_under():
    seen_ids = []
    seen_types = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            seen_ids.append(int(request.url.params["id"]))
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=7.5)]))
        body = json.loads(request.content)
        seen_ids.append(body["Events"][0]["GameId"])
        seen_types.append(body["Events"][0]["Type"])
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=751444117, home="A", away="B", stake=90, line=7.5, period=2, over=False,
        )
    )
    asyncio.run(executor.aclose())

    assert seen_ids == [751444119, 751444119]  # match_id + SECOND_HALF_ID_OFFSET
    assert seen_types == [10]
    assert result.success is True


def test_period_and_over_defaults_leave_pattern_1s_request_shape_unchanged():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event()]))
        body = json.loads(request.content)
        assert body["Events"][0]["GameId"] == 751444118  # match_id + FIRST_HALF_ID_OFFSET
        assert body["Events"][0]["Type"] == 9
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(match_id=751444117, home="A", away="B", stake=90, line=6.5)
    )
    asyncio.run(executor.aclose())

    assert result.success is True


def _double_chance_event(coef: float = 4.37, t: int = 4) -> dict:
    return {"T": t, "P": None, "G": 8, "C": coef}


def test_double_chance_bet_type_and_group_target_the_right_market():
    seen_ids = []
    seen_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            seen_ids.append(int(request.url.params["id"]))
            return httpx.Response(200, json=_game_zip_response([_double_chance_event()]))
        body = json.loads(request.content)
        seen_ids.append(body["Events"][0]["GameId"])
        seen_bodies.append(body["Events"][0])
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    from services.bettor.betting_api import DOUBLE_CHANCE_1X_T, DOUBLE_CHANCE_GROUP

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=751444117,
            home="A",
            away="B",
            stake=90,
            line=None,
            period=1,
            bet_type=DOUBLE_CHANCE_1X_T,
            group=DOUBLE_CHANCE_GROUP,
        )
    )
    asyncio.run(executor.aclose())

    assert seen_ids == [751444118, 751444118]  # match_id + FIRST_HALF_ID_OFFSET
    assert seen_bodies[0]["Type"] == 4
    # Confirmed via a real live Double Chance bet capture on 2026-09-14:
    # MakeBetWeb rejects Param=null for a lineless market with a 400 --
    # it wants 0. line=None only governs the read-side GetGameZip match
    # (P=null there is correct and unrelated).
    assert seen_bodies[0]["Param"] == 0
    assert result.success is True
    assert result.odds == 4.37


def test_double_chance_wrong_group_is_not_matched():
    def handler(request: httpx.Request) -> httpx.Response:
        # a Totals-shaped event with the same Type=4 but the Totals group --
        # must not be mistaken for the Double Chance selection.
        return httpx.Response(200, json=_game_zip_response([{"T": 4, "P": None, "G": 17, "C": 4.37}]))

    from services.bettor.betting_api import DOUBLE_CHANCE_1X_T, DOUBLE_CHANCE_GROUP

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=None,
            bet_type=DOUBLE_CHANCE_1X_T, group=DOUBLE_CHANCE_GROUP,
        )
    )
    asyncio.run(executor.aclose())

    assert result.success is False
    assert result.reason == "market not open (stale-fire guard)"


def test_bet_type_override_ignores_the_over_flag():
    # bet_type, when explicitly given, wins over whatever `over` would
    # otherwise have derived (over's default is True/TOTAL_OVER_T, but
    # Double Chance has no over/under concept at all).
    #
    # The MakeBetWeb body is captured here rather than asserted on inside
    # the handler: place_bet() wraps the whole request in a broad
    # `except Exception`, so an in-handler AssertionError would get
    # swallowed and reported as a misleading "request failed: ..."
    # BetResult instead of the real failure.
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_double_chance_event(t=6)]))
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    from services.bettor.betting_api import DOUBLE_CHANCE_2X_T, DOUBLE_CHANCE_GROUP

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=None, over=True,
            bet_type=DOUBLE_CHANCE_2X_T, group=DOUBLE_CHANCE_GROUP,
        )
    )
    asyncio.run(executor.aclose())

    assert result.success is True
    assert captured["body"]["Events"][0]["Type"] == 6


def test_period_0_targets_the_raw_match_id():
    seen_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            seen_ids.append(int(request.url.params["id"]))
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5)]))
        body = json.loads(request.content)
        seen_ids.append(body["Events"][0]["GameId"])
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=751444117, home="A", away="B", stake=90, line=16.5, period=0, over=False,
        )
    )
    asyncio.run(executor.aclose())

    assert seen_ids == [751444117, 751444117]  # raw match_id, no offset
    assert result.success is True


async def _instant_sleep(_seconds: float) -> None:
    return None


def test_min_odds_waits_for_odds_to_clear_the_threshold(monkeypatch):
    # Body asserted after asyncio.run(), not inside the handler -- place_bet()
    # wraps the whole request in a broad `except Exception`, so an in-handler
    # AssertionError would get swallowed and reported as a misleading
    # "request failed: ..." BetResult instead of the real failure (see
    # test_bet_type_override_ignores_the_over_flag's comment above).
    monkeypatch.setattr("services.bettor.betting_api.asyncio.sleep", _instant_sleep)
    odds_sequence = [1.2, 1.3, 1.6]
    calls = {"odds": 0}
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            coef = odds_sequence[min(calls["odds"], len(odds_sequence) - 1)]
            calls["odds"] += 1
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5, coef=coef)]))
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=16.5, period=0, over=False, min_odds=1.5,
        )
    )
    asyncio.run(executor.aclose())

    assert calls["odds"] == 3
    assert result.success is True
    assert result.odds == 1.6
    assert captured["body"]["Events"][0]["Coef"] == 1.6


def test_min_odds_already_met_places_immediately_without_is_stale():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/GetGameZip"):
            return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5, coef=1.8)]))
        return httpx.Response(
            200, json={"Value": {"Id": 1, "Balance": 910.0}, "Success": True, "Error": "", "ErrorCode": 0}
        )

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=16.5, period=0, over=False, min_odds=1.5,
        )
    )
    asyncio.run(executor.aclose())

    assert result.success is True
    assert result.odds == 1.8


def test_min_odds_never_reached_gives_up_once_stale(monkeypatch):
    # Odds never clear 1.5 in this test, so MakeBetWeb should never be
    # called -- the handler only ever needs to serve GetGameZip.
    monkeypatch.setattr("services.bettor.betting_api.asyncio.sleep", _instant_sleep)
    calls = {"odds": 0}
    stale_after = 3

    def handler(request: httpx.Request) -> httpx.Response:
        calls["odds"] += 1
        return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5, coef=1.2)]))

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=16.5, period=0, over=False,
            min_odds=1.5,
            is_stale=lambda: calls["odds"] >= stale_after,
        )
    )
    asyncio.run(executor.aclose())

    assert calls["odds"] == stale_after
    assert result.success is False
    assert "market closed before odds reached 1.5" in result.reason


def test_min_odds_none_never_sleeps(monkeypatch):
    async def _sleep_that_fails(_seconds: float) -> None:
        raise AssertionError("place_bet() must not sleep when min_odds is None")

    monkeypatch.setattr("services.bettor.betting_api.asyncio.sleep", _sleep_that_fails)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/GetGameZip")
        return httpx.Response(200, json=_game_zip_response([]))  # no matching market

    executor = _executor(handler)
    result = asyncio.run(executor.place_bet(match_id=1, home="A", away="B", stake=90, line=6.5))
    asyncio.run(executor.aclose())

    assert result.success is False
    assert result.reason == "market not open (stale-fire guard)"


def test_min_odds_max_wait_seconds_caps_the_loop_without_is_stale(monkeypatch):
    # No is_stale at all (and odds that never clear 1.5) -- without an
    # internal deadline this would poll forever. monkeypatch both
    # asyncio.sleep (instant, as elsewhere in this file) and time.monotonic
    # (a fake clock that jumps forward a fixed step per call) so the test
    # doesn't depend on real elapsed wall-clock time to prove the loop
    # actually gives up.
    monkeypatch.setattr("services.bettor.betting_api.asyncio.sleep", _instant_sleep)
    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr("services.bettor.betting_api.time.monotonic", fake_monotonic)
    calls = {"odds": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["odds"] += 1
        return httpx.Response(200, json=_game_zip_response([_matching_event(t=10, line=16.5, coef=1.2)]))

    executor = _executor(handler)
    result = asyncio.run(
        executor.place_bet(
            match_id=1, home="A", away="B", stake=90, line=16.5, period=0, over=False,
            min_odds=1.5,
            max_wait_seconds=250.0,
        )
    )
    asyncio.run(executor.aclose())

    # deadline = 100 (1st fake_monotonic call) + 250 = 350. Each loop
    # iteration fetches odds once then spends one more fake_monotonic call
    # on the deadline check (200, then 300, then 400) -- so the loop gives
    # up on the 3rd iteration, never running unbounded.
    assert calls["odds"] == 3
    assert result.success is False
    assert "odds never reached 1.5 within 250.0s" in result.reason
