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

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from logging import Logger

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

ACCESS_TOKEN_COOKIE = "access_token"

# Captured live against 1xbet.cm on 2026-09-13 -- see the design spec's
# "Investigation" section. Note: this site has duplicate DOM ids -- both
# #username and #username-password each match a non-input wrapper div in
# addition to the real <input>; the input# tag qualifier is required to
# disambiguate (discovered via a live supervised test on 2026-09-13, not
# visible from static DOM inspection alone since that only ever queried
# document.querySelectorAll('input')). Also: the login trigger button is
# itself a toggle (a second click closes what a first click opened) --
# login() guards its click behind a presence-check of the identifier
# field so a dropdown left open by a prior failed attempt is never
# accidentally closed instead of used (discovered via a second live
# supervised test on 2026-09-13).
LOGIN_TRIGGER_SELECTOR = "button.auth-dropdown-trigger"
IDENTIFIER_FIELD_SELECTOR = "input#username"

# A third live-testing round (still 2026-09-13) found the actual bug:
# #username/#username-password are the *generic* "E-mail or ID" fields.
# They do accept a raw phone number as typed text and the form does
# submit, but the real account is registered for phone-based login, and
# the site silently rejects it via that path ("Incorrect username or
# password!") no matter the phone-number format tried (national number
# alone, and with a +237 country-code prefix -- both rejected identically
# by that field). The form actually has a second, dedicated phone-login
# widget, reached by clicking a toggle icon (aria-label="Your phone
# number") next to the identifier field: it swaps in a #phone field
# (national number only -- country code is a separate pre-filled +237
# selector, not part of this field's value) and, importantly, a
# *different* password field, #phone-password, not #username-password.
# That toggle button is itself non-idempotent the same way the outer
# dropdown trigger is -- clicking it again once already in phone mode
# flips back to ID/email mode (confirmed live: its own aria-label changes
# to "ID or Email" once toggled) -- so login() guards this click behind a
# visibility-check of #phone, not a presence-check: unlike the outer
# dropdown (which mounts/unmounts on open/close), this inner form is
# always mounted and only ever toggles the CSS visibility of its two
# modes, so presence alone can't tell which mode is currently showing.
# #phone-password has the same wrapper-div id collision as
# #username-password (2 matches, input# tag-qualifier required); #phone
# has no such collision (1 match) but is tag-qualified anyway for
# consistency.
PHONE_TOGGLE_SELECTOR = 'button[aria-label="Your phone number"]'
PHONE_FIELD_SELECTOR = "input#phone"
PHONE_PASSWORD_FIELD_SELECTOR = "input#phone-password"
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

            if await page.query_selector(IDENTIFIER_FIELD_SELECTOR) is None:
                await page.click(LOGIN_TRIGGER_SELECTOR, timeout=5000)
            # An instant is_visible() check here would race the dropdown's
            # own post-click render/animation: if phone mode is ever the
            # form's default on open, a check taken before that render
            # settles could misread it as not-yet-visible and click the
            # (non-idempotent) toggle, flipping it back to ID/email mode.
            # wait_for_selector actively polls up to its own timeout instead
            # of sampling once, so a slow render is waited out rather than
            # misread; only a genuine ID/email-mode start (no such render
            # coming) times out to fall through to the toggle click. Uses
            # the same 5000ms timeout as every other selector interaction
            # in this function -- latency doesn't matter here (login()
            # runs in a background watchdog, never on the bet-placement
            # path), so there's no reason for this one to be shorter and
            # more failure-prone than the rest.
            try:
                await page.wait_for_selector(
                    PHONE_FIELD_SELECTOR, state="visible", timeout=5000
                )
            except PlaywrightTimeoutError:
                await page.click(PHONE_TOGGLE_SELECTOR, timeout=5000)
            await page.fill(PHONE_FIELD_SELECTOR, phone_number, timeout=5000)
            await page.fill(PHONE_PASSWORD_FIELD_SELECTOR, password, timeout=5000)
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


async def watchdog_loop(
    cdp_url: str,
    phone_number: str,
    password: str,
    check_interval_seconds: float,
    login_cooldown_seconds: float,
    login_timeout_seconds: float,
    log: Logger,
    *,
    is_alive: AliveChecker = is_session_alive,
    do_login: LoginFn = login,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Runs forever. Every check_interval_seconds, checks whether the
    session is alive; if it's dead and at least login_cooldown_seconds
    have passed since the last login attempt, calls do_login(...) and
    logs the outcome loudly either way. Returns immediately, without
    looping, if phone_number/password are unset -- there's nothing this
    watchdog can do without them, and it must never crash the bettor
    process over a missing optional feature."""
    if not phone_number or not password:
        log.warning(
            "session watchdog disabled: ONEXBET_PHONE_NUMBER/ONEXBET_PASSWORD not set"
        )
        return

    last_login_attempt: float | None = None
    while True:
        await sleep(check_interval_seconds)
        try:
            alive = await is_alive(cdp_url)
        except Exception as exc:  # noqa: BLE001 -- one bad check must not kill the watchdog
            log.error(f"session watchdog: alive-check failed: {exc}")
            continue
        if alive:
            continue

        current = now()
        if last_login_attempt is not None and (current - last_login_attempt) < login_cooldown_seconds:
            continue  # still in cooldown from a recent attempt

        last_login_attempt = current
        result = await do_login(cdp_url, phone_number, password, login_timeout_seconds)
        if result.success:
            log.info("AUTH RECOVERED — session watchdog logged back in successfully")
        else:
            log.error(f"AUTH LOGIN FAILED — manual login required: {result.reason}")
