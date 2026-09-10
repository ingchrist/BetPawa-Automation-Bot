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


def _matching_event(coef: float = 1.408, line: float = 6.5) -> dict:
    return {"T": 9, "P": line, "G": 17, "C": coef}


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
        assert body["Events"][0]["GameId"] == 751444117
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
