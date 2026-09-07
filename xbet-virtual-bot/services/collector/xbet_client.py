"""Async client for 1xbet's own public JSON feed API.

## Why this exists instead of driving a browser

The plan going in was to scrape the rendered page over CDP/Playwright (per
this machine's usual setup — see /home/cdjinguet/CLAUDE.md). That turned out
to be a dead end: 1xbet.cm's HTML routes are geo-blocked at the edge for
this box's egress IP (every page request 302s to /en/block, with response
header `x-gw-blk-redirect-reason: block`).

The site's own frontend, however, doesn't render odds by hand — it calls a
JSON API at `/service-api/LiveFeed/...` to get them, and *that* path is not
behind the same geo-block (verified: it 302-blocks "/", but 200s on
"/service-api/..." from the same IP). So this client talks to that API
directly. This is strictly better than DOM-scraping even where geo-blocking
isn't a factor: it's the same data the page itself uses, structured, with no
selectors to break when the site restyles.

## Endpoints (reverse-engineered by probing, 2026-09-07)

`GetChampsZip` — catalogue of every league/tournament, incl. its current
name. Not used for polling (too broad, changes rarely) — kept here only as
the reference for how `discover_target_leagues()` was derived; the real
per-poll filtering happens client-side against `Get1x2_VZip` below.

`Get1x2_VZip?sports={id}&count={n}&lng=en&getEmpty=true`
    Bulk feed: every currently live *and* about-to-start match for a sport.
    This is deliberately a wide net (all of FIFA, sport 85) rather than
    asking the API to filter by league — an unrecognized query param (tried
    `champs`, `champ`, `partner`, `top`) makes the whole request fail with
    HTTP 406, so the API's accepted parameter set is small and undocumented;
    narrower is safer to assume than it looks. League filtering happens in
    normalize.py instead.
    Does NOT include odds (`E` is always empty here) — just identity + score
    state. Cheap, so polled frequently.

`GetGameZip?id={match_id}&lng=en`
    Full detail for one match: same score/period state as above, PLUS the
    odds ladder in `E`. Market types seen in `E[].T` that matter to us:
        T=1/2/3  (G=1)   1X2 moneyline: home / draw / away
        T=9/10   (G=17)  Total Over / Total Under, paired by `P` (the line)
    Only called for matches already known-interesting (tracked by the
    collector's poll loop), not for the whole bulk feed — one call per
    tracked match per poll, not one per league.
"""
from __future__ import annotations

import httpx

MONEYLINE_HOME_T = 1
MONEYLINE_DRAW_T = 2
MONEYLINE_AWAY_T = 3
TOTAL_OVER_T = 9
TOTAL_UNDER_T = 10

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


class XbetClient:
    def __init__(self, api_base: str, timeout_seconds: float):
        self._client = httpx.AsyncClient(
            base_url=api_base, headers=DEFAULT_HEADERS, timeout=timeout_seconds
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_bulk_feed(self, sport_id: int, count: int = 100) -> list[dict]:
        """Every live/about-to-start match for `sport_id`, unfiltered by league."""
        resp = await self._client.get(
            "/LiveFeed/Get1x2_VZip",
            params={"sports": sport_id, "count": count, "lng": "en", "getEmpty": "true"},
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("Value") or []

    async def get_match_detail(self, match_id: int) -> dict | None:
        """Full detail (score by period + odds) for one match, or None if the
        API no longer has anything for that id (e.g. it aged out)."""
        resp = await self._client.get(
            "/LiveFeed/GetGameZip", params={"id": match_id, "lng": "en"}
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("Value")
