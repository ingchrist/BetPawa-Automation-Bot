"""BetExecutor: places real bets via 1xbet's own JSON API instead of
driving the betting UI with Playwright clicks.

## Why this replaced the Playwright-UI-driven design

The original plan (see the design spec's `BetExecutor` section) drove the
live page: navigate to the match, click the "1st half" tab, click "Over
6.5", fill the stake, click confirm. Live against the real site this proved
too flaky to trust with real money -- Vue-SPA re-render races on the tab
click, plus `connect_over_cdp` hanging once ~10 orphaned tabs accumulated
from earlier exploration. The user decided live to switch to the same
philosophy `services/collector/xbet_client.py` already uses for reads: call
the site's own JSON API directly.

## How the real request was reverse-engineered

One real, authorized capture bet (90 FCFA, "Total. 1st half", Over 6.5) was
placed manually while a raw-CDP network monitor (bypassing Playwright --
see below) watched the tab. That surfaced:

`POST /service-api/LiveBet/Secure/MakeBetWeb`
    Body: `{"UserId": <int>, "Events": [{"GameId": <first_half_game_id>,
    "Type": 9, "Coef": <current coefficient>, "Param": <line>, "PV": null,
    "PlayerId": 0, "Kind": 1, "InstrumentId": 0, "Seconds": 0, "Price": 0,
    "Expired": 0, "PlayersDuel": []}], "Vid": 0, "partner": 55,
    "Group": 654, "live": true, "CheckCf": 2, "Lng": "en", "notWait": true,
    "IsPowerBet": false, "Summ": <stake>, "isAutoBet": true, "autoBetCf": 0,
    "TransformEventKind": true, "autoBetCfView": 0, "Source": 55,
    "OneClickBet": 2}`
    `Type: 9` / `Param: <line>` matches the `TOTAL_OVER_T` / line-keyed
    encoding `xbet_client.py` already documents for reads. `CheckCf: 2`
    (coefficient-staleness tolerance) matches the verified working real
    request -- lower values are believed to hard-fail on any odds drift
    between fetch and placement, which would be actively harmful for a
    bot that always fetches-then-places a beat behind the live line.
    Response: `{"Value": {"Id": <bet id>, "Balance": <new balance>,
    "Coupon": {...}, ...}, "Success": true, "Error": "", "ErrorCode": 0}`.
    A failed placement is expected to carry `Success: false` and a
    populated `Error`/`ErrorCode` -- not yet observed live, so
    `place_bet()` treats any such response as failure conservatively.

Two auth headers ride on every call:

- `x-auth: Bearer <JWT>` -- verified byte-identical to the `access_token`
  cookie on the logged-in browser.
- `x-hd: <token>` -- verified byte-identical to the `.token` field inside
  `localStorage["fp_d"]` on the same page. That same blob carries its own
  `expiresAt`, landing within ~1 minute of the JWT's own `exp` -- both
  expire together (~4h window) and are auto-refreshed by the page's own JS
  as long as the browser stays open (there's a `refresh_token` cookie
  backing that, presumably used by the page itself, not by this client).

Because both tokens expire and refresh on their own inside the browser,
this client does not cache them -- it re-reads both fresh, via a read-only
CDP touch (cookies + one `localStorage.getItem`, no navigation, no new
tab), immediately before every bet. That touch is cheap (sub-second) and
doesn't fight over the tab the way full UI automation did.

## Why raw CDP instead of a second Playwright connection

Discovering the auth values required watching real network traffic while
the bet was placed manually. The first attempt used a second, independent
Playwright `connect_over_cdp` session as a passive observer -- reproduced
twice that it silently receives *zero* Network events triggered by another
session's activity on an already-open tab, even freshly attached with no
intervening navigation. A direct websocket connection to the tab's own
`webSocketDebuggerUrl` (raw CDP, no Playwright) does not have this gap --
verified by capturing both the page's own background polling and a
synthetic cross-session POST including its body. This module only needs a
single, short-lived Playwright connection per call (for `_read_auth`), so
it doesn't hit that gap -- it never has two sessions alive on the tab at
once.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

import httpx
from playwright.async_api import async_playwright

from services.collector.xbet_client import TOTAL_OVER_T, TOTAL_UNDER_T

# Corrected 2026-09-10, superseding the original "G=17 on the raw match_id
# is Total. 1st half" belief: each match actually spawns three separate
# sub-game ids -- the "main"/whole-match id (X, the one the collector and
# GetGameZip's bulk feed expose), the 1st-half id (X+1), and the 2nd-half
# id (X+2) -- each independently quotable via GetGameZip and each exposing
# its OWN period-scoped `G=17` Total Over/Under market. Betting against the
# raw match_id (X) is ambiguous: pregame/early it can coincide with the
# 1st-half market, but once the match is live it drifts toward the
# whole-match ("Main game") total instead -- confirmed by two controlled,
# real-money A/B checks (one live, one pregame) placing/inspecting bets
# against X, X+1, and X+2 on the same match side by side. X+1 and X+2 are
# unambiguous regardless of match phase, so betting always targets those
# directly instead of X.
FIRST_HALF_ID_OFFSET = 1
SECOND_HALF_ID_OFFSET = 2
TOTALS_GROUP = 17

# Double Chance -- confirmed live, read-only, against GetGameZip on
# matches in the FC 25. 3x3. Conference League (2860561): Group=8, with
# Type=4/5/6 keyed to 1X/12/2X respectively, Param always null (there's
# no line, unlike Totals). Verified against a live, undecided match
# (0-2 down at the time): 1X priced at 4.37 (unlikely but live), 2X
# priced at 1.001 (near-certain) -- consistent with the actual scoreline.
# Uses the identical match_id/+1/+2 sub-game-id scheme as Totals above.
# `group`/TOTALS_GROUP is a read-side-only disambiguator for
# _current_odds()'s GetGameZip filter -- MakeBetWeb's POST body never
# carries a Group field for either market (see place_bet() below: GameId,
# Type, Coef, Param only), so placement is uniquely addressed by
# (GameId, Type, Param) alone, Type being a flat global namespace across
# market groups for a given sub-game id. See
# docs/superpowers/specs/2026-09-13-first-half-winner-2x-streak-
# pattern-bettor-design.md's "Market mechanics" section for the full
# investigation, including the one residual risk this doesn't close:
# unlike TOTAL_OVER_T (confirmed via one real captured bet), this
# request shape is inferred by symmetry with Totals, not confirmed via
# an actual placed Double Chance bet.
DOUBLE_CHANCE_GROUP = 8
DOUBLE_CHANCE_1X_T = 4
DOUBLE_CHANCE_12_T = 5
DOUBLE_CHANCE_2X_T = 6


@dataclass(frozen=True)
class BetResult:
    success: bool
    reason: str | None = None
    odds: float | None = None


@dataclass(frozen=True)
class AuthTokens:
    access_token: str
    hd_token: str
    user_id: int


async def read_auth_from_browser(cdp_url: str) -> AuthTokens:
    """Read-only CDP touch: cookies + one localStorage key from the
    already-logged-in browser. No navigation, no new tab."""
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0]
        cookies = await ctx.cookies()
        by_name = {c["name"]: c["value"] for c in cookies}

        access_token = by_name.get("access_token")
        if not access_token:
            raise RuntimeError("no access_token cookie -- is the browser logged in?")

        user_id_raw = by_name.get("authenticated")
        if not user_id_raw:
            raise RuntimeError("no authenticated cookie -- can't determine user id")

        page = next((pg for pg in ctx.pages if "1xbet.cm" in pg.url), None)
        if page is None:
            raise RuntimeError("no 1xbet.cm tab open -- can't read localStorage")

        fp_d_raw = await page.evaluate("() => localStorage.getItem('fp_d')")
        if not fp_d_raw:
            raise RuntimeError("no fp_d entry in localStorage")
        hd_token = json.loads(fp_d_raw)["token"]

        return AuthTokens(access_token=access_token, hd_token=hd_token, user_id=int(user_id_raw))


AuthReader = Callable[[], Awaitable[AuthTokens]]


class BetExecutor:
    def __init__(
        self,
        api_base: str,
        cdp_url: str,
        timeout_seconds: float,
        auth_reader: AuthReader | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=api_base.rstrip("/"), timeout=timeout_seconds, transport=transport
        )
        self._auth_reader: AuthReader = auth_reader or (lambda: read_auth_from_browser(cdp_url))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def place_bet(
        self,
        match_id: int,
        home: str,
        away: str,
        stake: float,
        line: float | None = 6.5,
        period: Literal[1, 2] = 1,
        over: bool = True,
        bet_type: int | None = None,
        group: int = TOTALS_GROUP,
    ) -> BetResult:
        """`bet_type`/`group` default to Pattern 1/2's Total-market shape,
        derived from `over` exactly as before. Pass both explicitly (as
        Pattern 3 does, for the Double Chance market) to bet a market with
        no over/under concept at all -- in that case `over` is ignored."""
        offset = FIRST_HALF_ID_OFFSET if period == 1 else SECOND_HALF_ID_OFFSET
        game_id = match_id + offset
        if bet_type is None:
            bet_type = TOTAL_OVER_T if over else TOTAL_UNDER_T

        try:
            auth = await self._auth_reader()
        except Exception as exc:
            return BetResult(success=False, reason=f"auth read failed: {exc}")

        try:
            coef = await self._current_odds(game_id, line, bet_type, group)
        except Exception as exc:
            return BetResult(success=False, reason=f"odds lookup failed: {exc}")
        if coef is None:
            return BetResult(success=False, reason="market not open (stale-fire guard)")

        headers = {
            "x-auth": f"Bearer {auth.access_token}",
            "x-hd": auth.hd_token,
            "content-type": "application/json",
            "accept": "application/json, text/plain, */*",
            "x-requested-with": "XMLHttpRequest",
            "x-svc-source": "__BETTING_APP__",
            "x-app-n": "__BETTING_APP__",
        }
        body = {
            "UserId": auth.user_id,
            "Events": [
                {
                    "GameId": game_id,
                    "Type": bet_type,
                    "Coef": coef,
                    "Param": line,
                    "PV": None,
                    "PlayerId": 0,
                    "Kind": 1,
                    "InstrumentId": 0,
                    "Seconds": 0,
                    "Price": 0,
                    "Expired": 0,
                    "PlayersDuel": [],
                }
            ],
            "Vid": 0,
            "partner": 55,
            "Group": 654,
            "live": True,
            "CheckCf": 2,
            "Lng": "en",
            "notWait": True,
            "IsPowerBet": False,
            "Summ": stake,
            "isAutoBet": True,
            "autoBetCf": 0,
            "TransformEventKind": True,
            "autoBetCfView": 0,
            "Source": 55,
            "OneClickBet": 2,
        }

        try:
            resp = await self._client.post(
                "/LiveBet/Secure/MakeBetWeb", json=body, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return BetResult(success=False, reason=f"request failed: {exc}")

        if not data.get("Success"):
            reason = data.get("Error") or f"errorCode={data.get('ErrorCode')}"
            return BetResult(success=False, reason=reason, odds=coef)

        return BetResult(success=True, odds=coef)

    async def _current_odds(self, game_id: int, line: float | None, bet_type: int, group: int = TOTALS_GROUP) -> float | None:
        resp = await self._client.get(
            "/LiveFeed/GetGameZip", params={"id": game_id, "lng": "en"}
        )
        resp.raise_for_status()
        detail = resp.json().get("Value")
        if not detail:
            return None
        for event in detail.get("E") or []:
            if (
                event.get("T") == bet_type
                and event.get("P") == line
                and event.get("G") == group
            ):
                return event.get("C")
        return None
