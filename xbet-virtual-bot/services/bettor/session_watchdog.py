"""services/bettor/session_watchdog.py -- detects when the browser's
1xbet.cm session has died (the access_token cookie is gone) and logs
back in automatically, so BetExecutor.place_bet() -- untouched by this
module -- always finds a valid session when it reads auth fresh before a
bet. Runs as an independent background task in services/bettor/main.py,
never inside the bet-placement path itself: recovery happens proactively,
ahead of any bet attempt, so it never adds latency or a new failure mode
to the money-critical moment.

Login must go through the real browser (not a crafted API POST the way
place_bet() calls MakeBetWeb) -- see the design spec's "Investigation"
section for why: the real login POST (/web-api/user/auth) carries a
167,203-character `comp_d` field, a client-side-generated device/
behavioral fingerprint that only the site's own unmodified page JS can
produce. Driving the real form lets that JS generate it exactly as it
would for a human.
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import Logger

from playwright.async_api import async_playwright

ACCESS_TOKEN_COOKIE = "access_token"

# Captured live against 1xbet.cm on 2026-09-13 -- see the design spec's
# "Investigation" section. The identifier field (#username) accepts a
# phone number despite its "E-mail or ID" placeholder; the frontend
# itself splits whatever's typed into the phone_number/country_code
# fields the real login POST expects.
LOGIN_TRIGGER_SELECTOR = "button.auth-dropdown-trigger"
IDENTIFIER_FIELD_SELECTOR = "#username"
PASSWORD_FIELD_SELECTOR = "#username-password"
SUBMIT_BUTTON_SELECTOR = ".auth-form-fields__submit"


@dataclass(frozen=True)
class LoginResult:
    success: bool
    reason: str | None = None


async def is_session_alive(cdp_url: str) -> bool:
    """Cheap, read-only check: is there a live access_token cookie on the
    browser right now? No localStorage read (unlike
    read_auth_from_browser) -- not needed just to know the session is
    alive, and this runs far more often than an actual bet."""
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        ctx = browser.contexts[0]
        cookies = await ctx.cookies()
        return any(c["name"] == ACCESS_TOKEN_COOKIE for c in cookies)


async def login(
    cdp_url: str, phone_number: str, password: str, timeout_seconds: float = 15.0
) -> LoginResult:
    """Drives the real login form on the already-open 1xbet.cm tab: opens
    it, types phone_number + password into the real fields, submits, then
    polls for up to timeout_seconds for the access_token cookie to
    appear. Never raises -- always returns a LoginResult."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0]
            page = next((pg for pg in ctx.pages if "1xbet.cm" in pg.url), None)
            if page is None:
                return LoginResult(success=False, reason="no 1xbet.cm tab open")

            await page.click(LOGIN_TRIGGER_SELECTOR, timeout=5000)
            await page.fill(IDENTIFIER_FIELD_SELECTOR, phone_number, timeout=5000)
            await page.fill(PASSWORD_FIELD_SELECTOR, password, timeout=5000)
            await page.click(SUBMIT_BUTTON_SELECTOR, timeout=5000)

            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                cookies = await ctx.cookies()
                if any(c["name"] == ACCESS_TOKEN_COOKIE for c in cookies):
                    return LoginResult(success=True)
                await page.wait_for_timeout(500)
            return LoginResult(
                success=False,
                reason=f"access_token cookie did not appear within {timeout_seconds}s of submitting",
            )
    except Exception as exc:  # noqa: BLE001 -- a login attempt must never crash the watchdog
        return LoginResult(success=False, reason=f"login automation failed: {exc}")


AliveChecker = Callable[[str], Awaitable[bool]]
LoginFn = Callable[[str, str, str, float], Awaitable[LoginResult]]
