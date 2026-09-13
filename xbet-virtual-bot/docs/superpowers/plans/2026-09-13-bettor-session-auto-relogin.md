# Bettor Session Auto-Relogin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically detect when the bettor's browser session has been
logged out of 1xbet.cm and log back in without human intervention, so a
human only ever needs to log in manually when automated recovery itself
can't succeed.

**Architecture:** A new, fully independent background task
(`services/bettor/session_watchdog.py`) periodically checks whether the
browser's session cookie is still present and, if not, drives the real
login form (typing phone+password, submitting) on the same already-open
browser tab this bot has always assumed exists. Runs alongside the
existing `consume()` task in `services/bettor/main.py`. `BetExecutor`/
`place_bet()`/`read_auth_from_browser()` are not modified at all.

**Tech Stack:** Python 3.10, `asyncio`, Playwright (`connect_over_cdp`,
already a dependency), `pytest` + `pytest-asyncio` (already used by this
repo's test suite).

**Spec:** `docs/superpowers/specs/2026-09-13-bettor-session-auto-relogin-design.md`

## Global Constraints

- `services/bettor/betting_api.py` (`BetExecutor`, `place_bet()`,
  `read_auth_from_browser()`) is not modified by this work at all.
- The full existing test suite (`.venv/bin/pytest tests/ -v`) must still
  pass, unmodified in behavior, after every task.
- The watchdog never launches, navigates to, or opens a new browser tab
  — it only ever acts on the same already-open `1xbet.cm` tab, matching
  this feature's standing CDP rule (see the top-level `CLAUDE.md`).
- `ONEXBET_PHONE_NUMBER`/`ONEXBET_PASSWORD` are read from config the same
  way every other value in `shared/config.py` is; never logged, never
  included in any published event or `data/bets.log` line.
- **A real, live login attempt against the actual account (Task 6's Step
  3) is a supervised action, not something a fresh implementer subagent
  runs unattended.** It requires the human account holder to have
  populated real credentials into `.env` themselves and to be present
  watching the browser. Every other task's verification (module smoke
  imports, `is_session_alive()`'s read-only cookie check, the unit
  suite) is safe for a subagent to run autonomously.
- Login-form DOM selectors (`button.auth-dropdown-trigger`, `#username`,
  `#username-password`, `.auth-form-fields__submit`,
  `input[name=store_credentials]`) were captured live against the actual
  site on 2026-09-13 and are used verbatim below — see the design spec's
  "Investigation" section for how.

---

## Task 1: `session_watchdog.py` — `LoginResult`, `is_session_alive()`, `login()`

**Files:**
- Create: `services/bettor/session_watchdog.py`

**Interfaces:**
- Produces: `LoginResult(success: bool, reason: str | None = None)`,
  `async def is_session_alive(cdp_url: str) -> bool`,
  `async def login(cdp_url: str, phone_number: str, password: str, timeout_seconds: float = 15.0) -> LoginResult`.
  Task 2 consumes all three as `watchdog_loop`'s default `is_alive`/
  `do_login` implementations and its `LoginResult` return type.

These three are not unit-testable without a real browser connection —
same as `read_auth_from_browser()` in `services/bettor/betting_api.py`,
which this repo already accepts as verified live rather than via pytest
(see that module's docstring). This task's own verification is a live
smoke check instead of TDD, matching that precedent.

- [x] **Step 1: Write the module**

```python
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
```

(`AliveChecker`/`LoginFn` type aliases are defined here for Task 2 to
import and use in `watchdog_loop`'s signature.)

- [x] **Step 2: Smoke-test the module imports cleanly**

Run: `.venv/bin/python -c "import services.bettor.session_watchdog"`
Expected: no exception.

- [x] **Step 3: Live-verify `is_session_alive()` against the real browser (read-only, safe)**

With the CDP browser already running and logged in (check
`./run.sh status` first — see the top-level `CLAUDE.md`), run:

```bash
.venv/bin/python -c "
import asyncio
from services.bettor.session_watchdog import is_session_alive
print(asyncio.run(is_session_alive('http://127.0.0.1:9222')))
"
```

Expected: prints `True` while logged in. This does not touch `login()`
at all — it is a pure read, safe to run without any special supervision.

- [x] **Step 4: Commit**

```bash
git add services/bettor/session_watchdog.py
git commit -m "$(cat <<'EOF'
Add is_session_alive() and login() for automatic session recovery

Login must go through the real browser rather than a crafted API POST
(unlike place_bet()'s MakeBetWeb call) -- the real login endpoint
(/web-api/user/auth) carries a 167,203-character comp_d field, a
client-side device/behavioral fingerprint only the site's own page JS
can produce. login() drives the real form (captured live: the login
trigger, #username/#username-password fields, and submit button) so
that JS runs exactly as it would for a human.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01V88DTFEc5iCfgZQwVWiPes
EOF
)"
```

---

## Task 2: `watchdog_loop()` — scheduling and cooldown, fully TDD'd

**Files:**
- Modify: `services/bettor/session_watchdog.py`
- Test: `tests/test_session_watchdog.py`

**Interfaces:**
- Consumes: `LoginResult`, `AliveChecker`, `LoginFn`, `is_session_alive`,
  `login` (Task 1).
- Produces:
  `async def watchdog_loop(cdp_url: str, phone_number: str, password: str, check_interval_seconds: float, login_cooldown_seconds: float, login_timeout_seconds: float, log: Logger, *, is_alive: AliveChecker = is_session_alive, do_login: LoginFn = login, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, now: Callable[[], float] = time.monotonic) -> None`.
  Task 4 consumes this exact signature to wire it into `services/bettor/main.py`.

This is the pure-orchestration part (no real browser touch of its own),
fully unit-testable via the injected `is_alive`/`do_login`/`sleep`/`now`
callables — same dependency-injection shape `BetExecutor.__init__`
already uses for `auth_reader`.

- [x] **Step 1: Write the failing tests**

```python
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
```

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_session_watchdog.py -v`
Expected: FAIL — `watchdog_loop` doesn't exist yet (`ImportError`).

- [x] **Step 3: Implement `watchdog_loop`**

Append to `services/bettor/session_watchdog.py`:

```python
import asyncio  # add to the existing import block at the top of the file


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
```

- [x] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_session_watchdog.py -v`
Expected: 5/5 passing.

- [x] **Step 5: Run the full existing suite to confirm no regression**

Run: `.venv/bin/pytest tests/ -v`
Expected: all previously-passing tests still pass, plus the 5 new ones.

- [x] **Step 6: Commit**

```bash
git add services/bettor/session_watchdog.py tests/test_session_watchdog.py
git commit -m "$(cat <<'EOF'
Add watchdog_loop() scheduling logic for automatic session recovery

Pure orchestration over injectable is_alive/do_login/sleep/now callables,
matching BetExecutor's existing auth_reader dependency-injection shape.
Covers: a dead session triggers exactly one login attempt and then
respects the cooldown; an alive session never attempts login; a
persistently dead session retries once the cooldown expires; a
successful login does not repeat once the session reports alive again,
even if the cooldown would otherwise allow it; missing credentials make
the watchdog a no-op instead of looping forever.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01V88DTFEc5iCfgZQwVWiPes
EOF
)"
```

---

## Task 3: `ONEXBET_PHONE_NUMBER`/`ONEXBET_PASSWORD`/`AUTH_WATCHDOG_*` config knobs

**Files:**
- Modify: `shared/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `config.onexbet_phone_number: str`, `config.onexbet_password: str`,
  `config.auth_watchdog_enabled: bool`,
  `config.auth_watchdog_check_interval_seconds: float`,
  `config.auth_watchdog_login_cooldown_seconds: float`,
  `config.auth_watchdog_login_timeout_seconds: float`.
  Task 4 consumes all six.

- [x] **Step 1: Add the new fields to the `Config` dataclass**

In `shared/config.py`, in the `Config` dataclass, add these fields right
after the existing `cdp_url: str` field:

```python
    onexbet_phone_number: str
    onexbet_password: str
    auth_watchdog_enabled: bool
    auth_watchdog_check_interval_seconds: float
    auth_watchdog_login_cooldown_seconds: float
    auth_watchdog_login_timeout_seconds: float
```

- [x] **Step 2: Populate them in `load_config()`**

In `shared/config.py`'s `load_config()`, add these lines right after the
existing `cdp_url=os.environ.get("CDP_URL", "http://127.0.0.1:9222"),`
line, before the closing `)` of the `Config(...)` call:

```python
        onexbet_phone_number=os.environ.get("ONEXBET_PHONE_NUMBER", ""),
        onexbet_password=os.environ.get("ONEXBET_PASSWORD", ""),
        auth_watchdog_enabled=_bool("AUTH_WATCHDOG_ENABLED", True),
        auth_watchdog_check_interval_seconds=float(
            os.environ.get("AUTH_WATCHDOG_CHECK_INTERVAL_SECONDS", "60")
        ),
        auth_watchdog_login_cooldown_seconds=float(
            os.environ.get("AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS", "300")
        ),
        auth_watchdog_login_timeout_seconds=float(
            os.environ.get("AUTH_WATCHDOG_LOGIN_TIMEOUT_SECONDS", "15")
        ),
```

(Empty-string defaults for the two credential fields, not `None` — this
matches every other `str` field in `Config`, which is not `Optional`
anywhere. `watchdog_loop` already treats an empty/falsy string as
"unset".)

- [x] **Step 3: Verify the module imports and constructs cleanly**

Run: `.venv/bin/python -c "from shared.config import load_config; c = load_config(); print(c.auth_watchdog_enabled, c.auth_watchdog_check_interval_seconds)"`
Expected: prints `True 60.0` (or whatever `.env` currently overrides, if
anything) with no exception.

- [x] **Step 4: Run the full suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests still pass (no test constructs `Config(...)` directly
by hand, so these additive fields cannot have broken anything — but per
this feature's own global constraint, run the full suite anyway).

- [x] **Step 5: Document the new knobs in `.env.example`**

In `.env.example`, immediately before the final `PATTERN3_ENABLED=true`
line's section ends (i.e., append this as a new section at the end of
the file):

```
# --- Betting: automatic session recovery (services/bettor) ---
# Detects when the browser's 1xbet.cm session has been logged out and
# logs back in automatically, so you only ever need to log in by hand
# when automated recovery itself can't succeed (wrong credentials, an
# unexpected captcha/2FA step, or no browser tab open). See README.md
# "Automatic session recovery" before relying on this.
ONEXBET_PHONE_NUMBER=
ONEXBET_PASSWORD=
# Kill switch, mirroring PATTERN*_ENABLED's convention. Set false to
# disable auto-recovery entirely and go back to fully manual login.
AUTH_WATCHDOG_ENABLED=true
# How often the watchdog checks whether the session is still alive.
AUTH_WATCHDOG_CHECK_INTERVAL_SECONDS=60
# Minimum gap between consecutive login attempts, so a persistently
# broken login (wrong password, a UI change, an unexpected captcha)
# doesn't retry every interval forever.
AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS=300
# How long a single login attempt polls for the access_token cookie to
# appear after submitting the form before giving up.
AUTH_WATCHDOG_LOGIN_TIMEOUT_SECONDS=15
```

- [x] **Step 6: Commit**

```bash
git add shared/config.py .env.example
git commit -m "$(cat <<'EOF'
Add config knobs for automatic session recovery

ONEXBET_PHONE_NUMBER/ONEXBET_PASSWORD (empty by default -- the watchdog
no-ops with a warning until both are set) plus AUTH_WATCHDOG_ENABLED/
CHECK_INTERVAL_SECONDS/LOGIN_COOLDOWN_SECONDS/LOGIN_TIMEOUT_SECONDS,
mirroring the existing PATTERN*_ENABLED kill-switch convention.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01V88DTFEc5iCfgZQwVWiPes
EOF
)"
```

---

## Task 4: Wire the watchdog into `services/bettor/main.py`

**Files:**
- Modify: `services/bettor/main.py`

**Interfaces:**
- Consumes: `watchdog_loop` (Task 2), `config.onexbet_phone_number`,
  `config.onexbet_password`, `config.auth_watchdog_enabled`,
  `config.auth_watchdog_check_interval_seconds`,
  `config.auth_watchdog_login_cooldown_seconds`,
  `config.auth_watchdog_login_timeout_seconds` (Task 3).
- Produces: the bettor process now runs the watchdog as a fourth
  background concern (Pattern 1/2/3's `consume()` loop being the other
  three), started and cancelled the same way `consumer_task` already is.

This is the one task in this plan that touches the same file Pattern 3's
plan touched — read the whole file first (`services/bettor/main.py`) so
your edit lands correctly around the existing Pattern 1/2/3 code, which
you must not otherwise change.

- [x] **Step 1: Add the import**

In `services/bettor/main.py`, add this import alongside the existing
`from services.bettor.betting_api import (...)` import block (same
general import section near the top of the file):

```python
from services.bettor.session_watchdog import watchdog_loop
```

- [x] **Step 2: Start the watchdog task and log its startup line**

In `services/bettor/main.py`'s `run()`, find this existing block (it logs
Pattern 3's startup line — the last of the three `log.info("starting
Pattern N — ...")` calls):

```python
    log.info(
        f"starting Pattern 3 — streak_length={config.pattern3_streak_length} "
        f"trigger=2X selection=1X stake={config.pattern3_bet_stake_amount} "
        f"{'ENABLED' if config.pattern3_enabled else 'DISABLED (PATTERN3_ENABLED=false) — tracking only, will not bet'}"
    )
```

Immediately after it, add:

```python
    log.info(
        f"starting session watchdog — check_interval={config.auth_watchdog_check_interval_seconds:g}s "
        f"cooldown={config.auth_watchdog_login_cooldown_seconds:g}s "
        f"{'ENABLED' if config.auth_watchdog_enabled else 'DISABLED (AUTH_WATCHDOG_ENABLED=false)'}"
    )
```

- [x] **Step 3: Create and cancel the watchdog task alongside `consumer_task`**

Find this existing block near the end of `run()`:

```python
    consumer_task = asyncio.create_task(consume())
    await stop.wait()

    log.info("shutting down...")
    consumer_task.cancel()
    await executor.aclose()
    await bus.close()
```

Replace it with:

```python
    consumer_task = asyncio.create_task(consume())
    watchdog_task = (
        asyncio.create_task(
            watchdog_loop(
                config.cdp_url,
                config.onexbet_phone_number,
                config.onexbet_password,
                config.auth_watchdog_check_interval_seconds,
                config.auth_watchdog_login_cooldown_seconds,
                config.auth_watchdog_login_timeout_seconds,
                log,
            )
        )
        if config.auth_watchdog_enabled
        else None
    )
    await stop.wait()

    log.info("shutting down...")
    consumer_task.cancel()
    if watchdog_task is not None:
        watchdog_task.cancel()
    await executor.aclose()
    await bus.close()
```

- [x] **Step 4: Smoke-test the module imports and constructs cleanly**

Run: `.venv/bin/python -c "import services.bettor.main"`
Expected: no exception.

- [x] **Step 5: Run the full existing suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: all tests pass, same count as Task 2 (this task adds no new
test file — `main.py`'s `run()` has never had direct unit test coverage
in this repo, matching every prior pattern's own wiring task).

- [x] **Step 6: Commit**

```bash
git add services/bettor/main.py
git commit -m "$(cat <<'EOF'
Wire the session watchdog into services/bettor/main.py

Starts watchdog_loop() as a fourth background task alongside the
existing consume() loop, gated by AUTH_WATCHDOG_ENABLED, cancelled on
shutdown the same way consumer_task already is. Pattern 1/2/3's own
code and BetExecutor/place_bet() are untouched.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01V88DTFEc5iCfgZQwVWiPes
EOF
)"
```

---

## Task 5: Document automatic session recovery in `README.md`

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing new (documentation only).

- [x] **Step 1: Add an "Automatic session recovery" section**

In `README.md`, the "Betting patterns" section ends with Pattern 3's own
subsection, immediately followed by the "How data is sourced" heading.
Replace:

```markdown
Config knobs: `PATTERN3_STREAK_LENGTH`, `PATTERN3_BET_STAKE_AMOUNT`,
`PATTERN3_ENABLED` — see `.env.example`. Shares `CDP_URL` and
`BETS_LOG_PATH` with Pattern 1/2.

## How data is sourced
```

with:

```markdown
Config knobs: `PATTERN3_STREAK_LENGTH`, `PATTERN3_BET_STAKE_AMOUNT`,
`PATTERN3_ENABLED` — see `.env.example`. Shares `CDP_URL` and
`BETS_LOG_PATH` with Pattern 1/2.

## Automatic session recovery

The bettor process runs a background watchdog
(`services/bettor/session_watchdog.py`) alongside its three betting
patterns. Every `AUTH_WATCHDOG_CHECK_INTERVAL_SECONDS` (default 60), it
checks whether the browser's 1xbet.cm session is still alive (the same
`access_token` cookie check `BetExecutor` already relies on). If it's
gone, the watchdog logs back in automatically by driving the real login
form on the already-open browser tab — typing the phone number and
password configured in `ONEXBET_PHONE_NUMBER`/`ONEXBET_PASSWORD`,
submitting, and confirming the session cookie reappears.

This exists because 1xbet logs sessions out on its own from time to
time, and until now the only fix was a human noticing and logging back
in by hand — which has already cost at least one missed bet live. Login
has to go through the real browser rather than a direct API call (unlike
how bets themselves are placed): the real login request carries a large,
client-side-generated device fingerprint that only the site's own page
JS can produce.

**You should still log in manually** when the watchdog logs
`AUTH LOGIN FAILED` — that means automated recovery itself couldn't
succeed (wrong stored credentials, an unexpected captcha/2FA step, or no
browser tab open), and it will keep failing at the same rate
(`AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS`, default every 5 minutes) until
you do.

Config knobs: `ONEXBET_PHONE_NUMBER`, `ONEXBET_PASSWORD`,
`AUTH_WATCHDOG_ENABLED`, `AUTH_WATCHDOG_CHECK_INTERVAL_SECONDS`,
`AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS`,
`AUTH_WATCHDOG_LOGIN_TIMEOUT_SECONDS` — see `.env.example`. Set
`AUTH_WATCHDOG_ENABLED=false` to disable this entirely and go back to
fully manual login, the same as before this feature existed.

## How data is sourced
```

(Note the repeated `## How data is sourced` heading at the end of the
"with" block — this replace reattaches the heading you're inserting
before, since it was included in the "Replace" block above to anchor the
insertion point precisely.)

- [x] **Step 2: Update the `CDP_URL` config-table row and add the new knobs**

`CDP_URL`'s existing description says the CDP touch is "read-only... not
UI automation" — no longer fully accurate once the watchdog exists, since
it drives real form input for login recovery (bet placement itself is
still the read-only, API-based touch `CDP_URL`'s description originally
meant). In `README.md`'s "Configuration reference" table, replace:

```markdown
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the already-logged-in browser the bettor reads fresh auth from (read-only touch, not UI automation). |
| `BETS_LOG_PATH` | `data/bets.log` | Human-readable audit trail of every pattern fire / bet placed / failed / settled — tracked in git like `RESULT_LOG_PATH`. |
```

with:

```markdown
| `CDP_URL` | `http://127.0.0.1:9222` | Chrome DevTools Protocol endpoint for the already-logged-in browser. Bet placement itself only ever reads fresh auth from it (cookies/localStorage, no UI automation); the session watchdog is the one thing that drives real form input on it, for login recovery only — see [Automatic session recovery](#automatic-session-recovery). |
| `BETS_LOG_PATH` | `data/bets.log` | Human-readable audit trail of every pattern fire / bet placed / failed / settled — tracked in git like `RESULT_LOG_PATH`. |
| `ONEXBET_PHONE_NUMBER` / `ONEXBET_PASSWORD` | *(empty)* | Login credentials for automatic session recovery. Both required for the watchdog to do anything; see [Automatic session recovery](#automatic-session-recovery). |
| `AUTH_WATCHDOG_ENABLED` | `true` | Kill switch for the session watchdog only — set `false` to disable auto-recovery entirely and go back to fully manual login. |
| `AUTH_WATCHDOG_CHECK_INTERVAL_SECONDS` | `60` | How often the watchdog checks whether the session is still alive. |
| `AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS` | `300` | Minimum gap between consecutive login attempts. |
| `AUTH_WATCHDOG_LOGIN_TIMEOUT_SECONDS` | `15` | How long a single login attempt polls for the session cookie to appear before giving up. |
```

- [x] **Step 3: Proofread**

Run: `grep -n "session watchdog\|Automatic session recovery\|AUTH_WATCHDOG" README.md`
and confirm every reference reads correctly in context.

- [x] **Step 4: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document automatic session recovery in README

Adds the "Automatic session recovery" section: what the watchdog does,
why login has to go through the real browser instead of a direct API
call, when you still need to log in manually, and the new config knobs.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01V88DTFEc5iCfgZQwVWiPes
EOF
)"
```

---

## Task 6: Final regression + one supervised live login test

**Files:** none (verification only).

- [x] **Step 1: Run the full test suite one more time from a clean state**

Run: `.venv/bin/pytest tests/ -v`
Expected: every test passes, including the 5 new ones from Task 2. Note
the final total.

- [x] **Step 2: Confirm `git log` shows a clean, complete task sequence**

Run: `git log --oneline -6`
Expected: one commit per task above (Tasks 1-5 — this task makes no file
changes).

- [x] **Step 3: A supervised, real login test — controller-run, not a fresh subagent**

**This step is not for an autonomous implementer to run unattended.** It
requires real credentials and a human watching the actual browser. If
you are a subagent executing this plan, STOP here and hand this step
back to the controller/human rather than attempting it yourself.

Coordinate with the account holder to:
1. Confirm `ONEXBET_PHONE_NUMBER` and `ONEXBET_PASSWORD` are populated in
   `.env` with real values (the human does this themselves — an agent
   should never ask for or type the password).
2. Ask the human to log out of 1xbet.cm in the browser.
3. With them watching, run:

```bash
.venv/bin/python -c "
import asyncio
from shared.config import load_config
from services.bettor.session_watchdog import login

config = load_config()
result = asyncio.run(login(config.cdp_url, config.onexbet_phone_number, config.onexbet_password))
print(result)
"
```

Expected: `LoginResult(success=True, reason=None)`, and the human
confirms visually that the browser is now logged in. If it instead
returns `success=False`, read `reason` — it will say either that no tab
was found, that the `access_token` cookie never appeared within the
timeout (check whether the login form's fields still match the selectors
in `session_watchdog.py` — the site may have changed since 2026-09-13),
or a raised-exception message from the automation itself. Do not treat a
failure here as this task's own bug to silently patch — surface it to
the human and use systematic-debugging if the cause isn't obvious from
`reason` alone.

- [x] **Step 4: Restart the bettor process and confirm the watchdog starts cleanly**

Follow this repo's own runbook (`./run.sh status`, or restart just the
`bettor` process the way this feature's own prior session did — see the
top-level `CLAUDE.md`) to restart `services/bettor/main.py` onto the new
code. Confirm the log shows the new `starting session watchdog — ...`
line (ending in `ENABLED` if `AUTH_WATCHDOG_ENABLED` is unset/true), and
that Pattern 1/2/3's own startup lines are unchanged. No code changes are
expected from this step.
