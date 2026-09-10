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
