"""Bambu Cloud authentication — real login + email-verification flow.

Bambu Lab does NOT expose a static API token on their account page. A token
is obtained by logging in through their (unofficial, reverse-engineered)
cloud auth flow:

1. POST email + password to ``/v1/user-service/user/login``.
2. Depending on account security, Bambu responds with:
   - ``loginType == "verifyCode"`` → it emails a code; we POST it back
     (after first triggering ``/sendemail/code``) to complete login.
   - ``loginType == "tfa"`` → a TOTP/MFA code is required.
   - ``success == true`` with an ``accessToken`` → no second factor.
3. On success we receive a Cloud Access Token (valid for months).

All of that is handled by ``bambulab.auth.BambuAuthenticator`` — this module
just drives the interactive UX (prompts on stderr) and adapts the result to
PrintQueue's config. The token is also persisted by the library to
``~/.printqueue/bambu_token`` as a side effect.

If the interactive flow can't complete, the raised ``AuthError`` includes
the manual browser-devtools token-extraction fallback.

Reference: https://github.com/coelacant1/bambu-lab-cloud-api
"""
from __future__ import annotations

import getpass
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

AUTH_TIMEOUT = 30.0
TOKEN_FILE = Path.home() / ".printqueue" / "bambu_token"

_MANUAL_FALLBACK = (
    "\nIf login keeps failing, extract the token manually:\n"
    "  1. Log into https://bambulab.com in your browser\n"
    "  2. Open DevTools → Network tab\n"
    "  3. Find a request to api.bambulab.com → copy the Authorization "
    "header (the part after 'Bearer ')\n"
    "  4. Run: pq config --token <TOKEN>"
)


class AuthError(Exception):
    """Raised when Bambu Cloud auth fails for any reason."""


@dataclass
class LoginResult:
    """Result of an interactive login."""
    token: str
    uid: Optional[str] = None


def _region_key(region: str) -> str:
    return "china" if str(region).lower() in ("china", "cn") else "global"


def _import_bambulab():
    """Lazy import so a queue-only install (no bambu-lab-cloud-api) still runs."""
    try:
        from bambulab.auth import BambuAuthenticator, BambuAuthError
        from bambulab.client import BambuClient
    except ImportError as exc:
        raise AuthError(
            "bambu-lab-cloud-api is not importable from this interpreter "
            f"({sys.executable}): {exc}\n"
            "It is installed in the project .venv — run via ./pq, or: "
            "pip install bambu-lab-cloud-api"
        ) from exc
    return BambuAuthenticator, BambuAuthError, BambuClient


# ---- Interactive login flow -----------------------------------------------

def interactive_login(
    region: str = "global",
    timeout: float = AUTH_TIMEOUT,
) -> LoginResult:
    """Run the full interactive Bambu Cloud login flow.

    Prompts (email/password/verification code) go to stderr; the code is
    read via ``input()`` and the password via ``getpass``. Delegates the
    actual protocol to ``BambuAuthenticator``.

    Returns a ``LoginResult`` with the access token and (best-effort) uid.

    Raises ``AuthError`` on any failure.
    """
    BambuAuthenticator, BambuAuthError, BambuClient = _import_bambulab()

    print("\n🔐 Bambu Lab Cloud Login", file=sys.stderr)
    print("─" * 40, file=sys.stderr)
    email = input("  Email: ").strip()
    if not email:
        raise AuthError("Email is required")
    password = getpass.getpass("  Password: ")
    if not password:
        raise AuthError("Password is required")

    def code_callback() -> str:
        print("  📧 Bambu sent a verification code (check your email / authenticator).",
              file=sys.stderr)
        return input("  Enter code: ").strip()

    # The library persists the token to token_file; make sure its dir exists.
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    authenticator = BambuAuthenticator(
        region=_region_key(region),
        token_file=str(TOKEN_FILE),
    )
    # Respect the requested timeout for the library's HTTP session.
    try:
        authenticator.session.request = _with_timeout(  # type: ignore[assignment]
            authenticator.session.request, timeout
        )
    except Exception:
        pass  # non-fatal — library has its own 30s default

    print("\n  Sending login request…", file=sys.stderr)
    try:
        token = authenticator.login(email, password, code_callback)
    except BambuAuthError as exc:
        raise AuthError(f"Login failed: {exc}{_MANUAL_FALLBACK}") from exc
    except Exception as exc:
        raise AuthError(f"Unexpected login error: {exc}{_MANUAL_FALLBACK}") from exc

    if not token:
        raise AuthError("Login returned no token." + _MANUAL_FALLBACK)

    print("  ✅ Authentication successful!", file=sys.stderr)

    # Best-effort: fetch uid (MQTT username) so api.py needn't re-resolve it.
    uid: Optional[str] = None
    try:
        client = BambuClient(token)
        if _region_key(region) == "china":
            client.BASE_URL = "https://api.bambulab.cn"
        info = client.get_user_info()
        if isinstance(info, dict) and info.get("uid") is not None:
            uid = str(info["uid"])
    except Exception as exc:
        print(f"  ⚠ Could not fetch account uid ({exc}); it will be resolved "
              f"automatically on first status poll.", file=sys.stderr)

    return LoginResult(token=token, uid=uid)


def _with_timeout(request_fn, timeout: float):
    """Wrap requests.Session.request to inject a default timeout."""
    def wrapped(*args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return request_fn(*args, **kwargs)
    return wrapped


# ---- Token validation ------------------------------------------------------

def validate_token(token: str, region: str = "global", timeout: float = AUTH_TIMEOUT) -> bool:
    """Return True if the token is still valid (GET /v1/user-service/my/profile)."""
    if not token:
        return False
    try:
        BambuAuthenticator, _BambuAuthError, _BambuClient = _import_bambulab()
    except AuthError:
        return False
    try:
        authenticator = BambuAuthenticator(
            region=_region_key(region),
            token_file=str(TOKEN_FILE),
        )
        return bool(authenticator.verify_token(token))
    except Exception:
        return False
