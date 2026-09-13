# Design: automatic session recovery for the bettor

## Problem

`services/bettor/betting_api.py`'s `BetExecutor` has always assumed a
human keeps an already-logged-in 1xbet.cm browser tab open, reading fresh
auth (an `access_token` cookie + an `hd_token` from `localStorage`) via a
read-only CDP touch immediately before every bet — see that module's own
docstring for the full rationale. That assumption breaks when 1xbet logs
the session out on its own (observed live: `bet failed for match ...:
auth read failed: no access_token cookie -- is the browser logged in?`),
which has already caused at least one live `PatternArmed` to fail to
place. Today the only fix is a human noticing and logging back in by
hand. This spec adds automatic recovery: detect the dead session and log
back in automatically, using stored credentials, with manual login as the
fallback only when automated recovery itself can't succeed.

## Investigation: the actual login request

Captured live via the same read-only raw-CDP network-monitor technique
already used in this repo to reverse-engineer `MakeBetWeb`
(`scratch/raw_cdp_monitor.py`; this investigation's own copy is
`scratch/capture_login.py`) while the account holder logged out and back
in manually. Findings:

- **Endpoint:** `POST https://1xbet.cm/web-api/user/auth`
- **Request body:** `{"phone_number": <base64>, "country_code": "237",
  "uPassword": <base64>, "save": true, "comp_d": <opaque blob>}`.
  `phone_number`/`uPassword` are base64-encoded, unremarkable in size.
- **`comp_d` is 167,203 characters long.** This is not a value that can
  be constructed by hand or reverse-engineered into a static template the
  way `MakeBetWeb`'s body was — its size and structure are consistent
  with a client-side-generated device/behavioral fingerprint (the kind of
  payload anti-bot vendors' obfuscated JS produces), almost certainly
  unique per page load and not something a bare `httpx.post()` could ever
  reproduce correctly.
- **Response:** `200 {"success": true, "type": 0, "message": "",
  "need_reload": false, "user": null, "token": <557-char string>}`. No
  `Set-Cookie` header appeared on this response in the capture — the
  actual `access_token`/`authenticated` cookies must be established by
  the page's own subsequent JS (using the returned `token`) rather than
  directly by this response. This client never needs to reproduce that
  step itself, since it only ever reads whatever cookies end up in the
  browser afterward (see Architecture below) — it doesn't need to
  understand how they got there.
- **No captcha or OTP/2FA step appeared** in this one captured login, on
  the same long-running, already-trusted browser/device this bot has
  always used. This is not a guarantee it can never appear (see Residual
  risks), but it means the common case is a plain phone+password
  round-trip.

**Conclusion driving the whole design:** because of the `comp_d` blob,
login must happen *through the real browser* — letting 1xbet's own
unmodified page JS generate that blob naturally, the exact same way it
does for a human — rather than attempting to replay a crafted JSON
request the way `place_bet()` does for `MakeBetWeb`. This is plain
browser-form automation (typing into real fields, clicking a real
button), the same category of thing this repo's own Pattern 1 design
originally attempted for bet placement, just applied to a form that:

- Is not itself time/odds-sensitive (unlike a live bet slip, a login
  doesn't go stale in seconds), so it doesn't carry the flakiness risk
  that got UI-driven bet placement abandoned.
- Is being submitted from an already-recognized device/browser profile,
  where the observed capture needed no additional verification step.

## Global constraints

- `services/bettor/betting_api.py` (`BetExecutor`, `place_bet()`,
  `read_auth_from_browser()`) is not modified by this work at all. The
  watchdog is purely additive and runs alongside it.
- The full existing test suite must still pass, unmodified in behavior,
  after every task implementing this spec.
- The watchdog never launches, navigates, or opens a new browser tab —
  it only ever acts on the same already-open `1xbet.cm` tab this bot has
  always assumed exists, matching this feature's standing CDP rule.
- Credentials (`ONEXBET_PHONE_NUMBER`/`ONEXBET_PASSWORD`) are read from
  config the same way every other value in `shared/config.py` is; never
  logged, never included in any published event or `data/bets.log` line.

## Architecture

A new module, `services/bettor/session_watchdog.py`, entirely independent
of `BetExecutor`/`place_bet()`:

```python
def is_session_alive(cdp_url: str) -> bool  # async
    """Cheap cookie-presence check -- same technique as
    read_auth_from_browser()'s access_token check, minus the
    localStorage read (not needed just to know the session is alive)."""

@dataclass(frozen=True)
class LoginResult:
    success: bool
    reason: str | None = None

async def login(cdp_url: str, phone_number: str, password: str) -> LoginResult
    """Drives the real login form in the browser: opens it, types
    phone_number + password, submits, polls for up to
    AUTH_WATCHDOG_LOGIN_TIMEOUT_SECONDS for the access_token cookie to
    appear. Returns success/failure with a human-readable reason on
    failure -- never raises."""

async def watchdog_loop(
    cdp_url: str,
    phone_number: str | None,
    password: str | None,
    check_interval_seconds: float,
    login_cooldown_seconds: float,
    log: Logger,
    *,
    is_alive: Callable[[str], Awaitable[bool]] = is_session_alive,
    do_login: Callable[[str, str, str], Awaitable[LoginResult]] = login,
) -> None
    """Runs forever. Every check_interval_seconds, checks is_alive(cdp_url);
    if the session is dead and at least login_cooldown_seconds have passed
    since the last login attempt, calls do_login(...) and logs the outcome
    loudly either way. is_alive/do_login are injectable for testing, same
    pattern BetExecutor already uses for auth_reader."""
```

Wired into `services/bettor/main.py`'s `run()` as one more
`asyncio.create_task(...)`, started and cancelled the same way the
existing `consumer_task` already is. **`place_bet()` and
`read_auth_from_browser()` are not touched at all** — the watchdog is a
parallel background task, not a fallback branch inside the bet-placement
path. This is deliberate: the money-critical hot path (already patched
and re-reviewed once this same day for an unrelated bug) stays exactly as
it is, and login recovery happens ahead of time, never adding latency or
a new failure mode to an actual bet attempt.

## Configuration

New `.env` knobs, following this repo's existing naming/documentation
convention (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `ONEXBET_PHONE_NUMBER` | *(none)* | The account's login phone number. Required for the watchdog to do anything. |
| `ONEXBET_PASSWORD` | *(none)* | The account's login password. Required for the watchdog to do anything. |
| `AUTH_WATCHDOG_ENABLED` | `true` | Kill switch, mirroring `PATTERN*_ENABLED`'s convention. Set `false` to disable auto-recovery entirely and go back to fully manual login. |
| `AUTH_WATCHDOG_CHECK_INTERVAL_SECONDS` | `60` | How often the watchdog checks whether the session is still alive. |
| `AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS` | `300` | Minimum gap between consecutive login attempts, so a persistently broken login (wrong password, a UI change, an unexpected captcha) doesn't retry every interval forever. |
| `AUTH_WATCHDOG_LOGIN_TIMEOUT_SECONDS` | `15` | How long `login()` polls for the `access_token` cookie to appear after submitting the form before giving up and returning failure. |

If either `ONEXBET_PHONE_NUMBER` or `ONEXBET_PASSWORD` is unset, the
watchdog logs one warning at startup and does nothing thereafter — it
never crashes the bettor process over missing credentials, and every
other pattern/service keeps working exactly as today.

## Error handling

- **Login attempt fails** (wrong credentials, an unexpected page state,
  a captcha/OTP step the one successful capture didn't show, a timeout
  waiting for the cookie to appear): `login()` returns
  `LoginResult(success=False, reason=...)`, never raises. The watchdog
  logs a clear `AUTH LOGIN FAILED — manual login required: <reason>`
  line and respects the cooldown before trying again. No crash, no
  silent retry storm.
- **No `1xbet.cm` tab open at all** (browser closed): `is_session_alive`
  and `login` both fail the same way `read_auth_from_browser` already
  does today — logged, not crashed. The watchdog never launches a new
  browser (matches this feature's standing CDP rule: never launch or
  navigate a new browser/tab this bot doesn't already have open — login
  is driven on the existing tab only).
- **Successful recovery**: logs `AUTH RECOVERED` (or similar), and the
  next `place_bet()` call proceeds exactly as it would have if the
  session had never dropped — it re-reads cookies fresh, same as always.

## Testing

`watchdog_loop`'s scheduling/cooldown logic is pure orchestration and
unit-testable with injected fake `is_alive`/`do_login` callables — same
dependency-injection shape `BetExecutor.__init__`'s optional `auth_reader`
already uses, and the same pattern `tests/test_betting_api.py` already
exercises. Tests should cover at minimum: a dead session triggers exactly
one login attempt; a second dead-session check within the cooldown window
does not trigger a second attempt; a session that's alive never attempts
login; a successful login attempt does not repeat on the next check even
after the cooldown (only a *newly* dead session should trigger another
attempt).

`is_session_alive` and `login` themselves are not unit-testable any more
than `read_auth_from_browser` is today — both require a real browser
connection. Verified live instead, same precedent this codebase already
follows for every CDP-touching function.

## Residual risks, explicitly accepted

- **A captcha or OTP/2FA step could appear on some future login attempt**
  even though this investigation's one captured login didn't need one.
  If that happens, `login()` fails cleanly (it won't recognize the
  unexpected page state, will time out waiting for the `access_token`
  cookie that never appears, and returns a failure) — falling back to
  manual login exactly as the user asked for ("log in manually if and
  only if the cookies expired"), not a crash or a hang.
- **Repeated automated logins could theoretically draw the site's own
  anti-bot/rate-limiting attention** in a way a human logging in
  occasionally wouldn't. The cooldown (`AUTH_WATCHDOG_LOGIN_COOLDOWN_SECONDS`,
  default 5 minutes) exists specifically to bound how often this bot ever
  attempts a login, even under a persistently dead session.
- **Credentials live in `.env` in plaintext**, consistent with how every
  other secret-shaped value in this repo is handled (`.env` is
  gitignored; there is no existing secrets-manager convention in this
  project to extend instead).
- **Exact DOM selectors for the login form are not yet known** — the
  modal isn't mounted in the page's DOM while already logged in, so this
  investigation could not inspect it without another logout. Capturing
  them (via a further, brief supervised logout, same technique) is
  implementation work, not a design decision this spec needs to settle.
