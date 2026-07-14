"""Bambu Cloud authentication — login + 2FA/verification code flow.

Bambu Lab does NOT expose a static API token on their account page.
Authentication requires an interactive login flow:

1. POST email + password to the auth endpoint → get a verification challenge
2. User receives a verification code via email (or 2FA TOTP)
3. POST the code back → receive a Cloud Access Token (~3 months valid)

This module provides both an interactive CLI login flow and the ability
to accept a token directly if the user already has one (extracted via
browser dev tools, a third-party tool, etc.).

References:
- https://github.com/coelacant1/bambu-lab-cloud-api (Python lib with full login+MQTT)
- Community-reversed Bambu Cloud auth flow (documented via traffic analysis)
"""
from __future__ import annotations

import getpass
import sys
from typing import Optional

import httpx

AUTH_BASE = "https://api.bambulab.com"
AUTH_TIMEOUT = 10.0


class AuthError(Exception):
    """Raised when Bambu Cloud auth fails for any reason."""


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "PrintQueue/0.1",
    }


# ---- Interactive login flow -----------------------------------------------

def interactive_login(
    region: str = "global",
    timeout: float = AUTH_TIMEOUT,
) -> str:
    """Run the full interactive Bambu Cloud login flow.

    Steps:
    1. Prompt for email + password
    2. Send login request → get verification challenge
    3. Prompt for verification code (email or TOTP)
    4. Exchange code for access token

    Returns the access token string.

    Raises AuthError on any failure.
    """
    base = _base_for_region(region)

    # Step 1: credentials
    print("\n🔐 Bambu Lab Cloud Login", file=sys.stderr)
    print("─" * 40, file=sys.stderr)
    email = input("  Email: ").strip()
    if not email:
        raise AuthError("Email is required")
    password = getpass.getpass("  Password: ")
    if not password:
        raise AuthError("Password is required")

    # Step 2: initiate login
    print("\n  Sending login request…", file=sys.stderr)
    try:
        with httpx.Client(timeout=timeout, headers=_headers()) as client:
            login_resp = client.post(
                f"{base}/v1/user-service/user/login",
                json={"account": email, "password": password},
            )
    except httpx.HTTPError as exc:
        raise AuthError(f"Login request failed: {exc}") from exc

    data = _parse_json(login_resp)
    if login_resp.status_code >= 400 or not isinstance(data, dict):
        msg = data.get("message", "") if isinstance(data, dict) else ""
        raise AuthError(f"Login rejected (HTTP {login_resp.status_code}): {msg}")

    # Bambu may use different responses depending on account security settings:
    # - "verifyCode" or "verificationCode" → email code needed
    # - "tfaKey" or "tfa_key" → TOTP 2FA needed
    # - "accessToken" present → no 2FA required (rare)

    tok = _extract_token(data)
    if tok:
        print("  ✅ Logged in (no 2FA required)", file=sys.stderr)
        return tok

    # Step 3: verification code
    code_method = "verification code (check your email)" if "tfa" not in str(data).lower() else "2FA/TOTP code"
    print(f"  📧 Bambu sent a {code_method}", file=sys.stderr)
    code = input("  Enter code: ").strip()
    if not code:
        raise AuthError("Verification code is required")

    # Step 4: verify
    print("  Verifying…", file=sys.stderr)
    verify_payload = _build_verify_payload(data, code)
    verify_path = _pick_verify_path(data)

    try:
        with httpx.Client(timeout=timeout, headers=_headers()) as client:
            verify_resp = client.post(
                f"{base}{verify_path}",
                json=verify_payload,
            )
    except httpx.HTTPError as exc:
        raise AuthError(f"Verification request failed: {exc}") from exc

    verify_data = _parse_json(verify_resp)
    if verify_resp.status_code >= 400 or not isinstance(verify_data, dict):
        msg = verify_data.get("message", "Unknown error") if isinstance(verify_data, dict) else "Unknown error"
        raise AuthError(f"Verification failed (HTTP {verify_resp.status_code}): {msg}")

    tok = _extract_token(verify_data)
    if not tok:
        # Some versions return a separate token exchange step
        tok = _extract_token(data)  # fallback to original response

    if not tok:
        raise AuthError("Could not extract access token from verification response.\n"
                        "Try extracting the token manually via browser dev tools:\n"
                        "  1. Log into https://bambulab.com in your browser\n"
                        "  2. Open DevTools → Network tab\n"
                        "  3. Look for requests to api.bambulab.com → copy the Authorization header\n"
                        "  4. Run: pq config --token <TOKEN>")

    print("  ✅ Authentication successful!", file=sys.stderr)
    return tok


def _base_for_region(region: str) -> str:
    region = region.lower()
    if region in ("china", "cn"):
        return "https://api.bambulab.cn"
    return "https://api.bambulab.com"


def _parse_json(resp: httpx.Response) -> dict | list | str | None:
    try:
        return resp.json()
    except Exception:
        return None


def _extract_token(data: dict) -> Optional[str]:
    """Probe common token field names across Bambu Cloud API versions."""
    if not isinstance(data, dict):
        return None
    for key in ("accessToken", "access_token", "token", "auth_token", "bearer_token"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Nested: some versions wrap it
    for sub in ("data", "result", "content"):
        inner = data.get(sub)
        if isinstance(inner, dict):
            tok = _extract_token(inner)
            if tok:
                return tok
    return None


def _build_verify_payload(login_data: dict, code: str) -> dict:
    """Construct verification payload based on what Bambu's login response includes."""
    payload: dict = {}

    # Carry forward the account identifier
    for key in ("account", "email", "username", "uid", "userId"):
        if key in login_data:
            payload["account"] = login_data[key]
            break

    # Different API versions use different field names for the code
    # Try to match what the login response suggests
    code_key = "code"
    if "tfaKey" in login_data or "tfa_key" in login_data:
        code_key = "tfaCode"
        payload["tfaKey"] = login_data.get("tfaKey") or login_data.get("tfa_key")
    elif "verifyKey" in login_data or "verify_key" in login_data:
        code_key = "verifyCode"
        payload["verifyKey"] = login_data.get("verifyKey") or login_data.get("verify_key")

    payload[code_key] = code
    return payload


def _pick_verify_path(data: dict) -> str:
    """Some Bambu endpoints use different verify paths."""
    if "tfaKey" in data or "tfa_key" in data:
        return "/v1/user-service/user/login"
    # Default: same login endpoint, different payload
    return "/v1/user-service/user/login"


# ---- Utility: test if a token is valid ------------------------------------

def validate_token(token: str, region: str = "global", timeout: float = AUTH_TIMEOUT) -> bool:
    """Check whether a token is still valid by hitting the devices endpoint."""
    base = _base_for_region(region)
    try:
        with httpx.Client(timeout=timeout, headers={
            **_headers(),
            "Authorization": f"Bearer {token}",
        }) as client:
            resp = client.get(f"{base}/v1/user-service/my/devices")
            return resp.status_code < 400
    except httpx.HTTPError:
        return False
