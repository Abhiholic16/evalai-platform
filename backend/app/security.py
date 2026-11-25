"""Small signed-cookie faculty session helper for the single-faculty demo."""
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time


COOKIE_NAME = "evalai_faculty_session"
SESSION_SECONDS = 8 * 60 * 60


def auth_settings() -> tuple[str, str, str]:
    username = os.getenv("EVALAI_FACULTY_USERNAME", "").strip()
    password = os.getenv("EVALAI_FACULTY_PASSWORD", "")
    secret = os.getenv("EVALAI_SESSION_SECRET", "")
    if not username or len(password) < 12 or password == username or len(secret) < 32 or secret == password:
        raise RuntimeError(
            "Faculty sign-in is not configured. Set EVALAI_FACULTY_USERNAME, "
            "a 12+ character EVALAI_FACULTY_PASSWORD, and a random 32+ character EVALAI_SESSION_SECRET."
        )
    return username, password, secret


def credentials_match(username: str, password: str) -> bool:
    configured_username, configured_password, _ = auth_settings()
    user_ok = hmac.compare_digest(username.encode("utf-8"), configured_username.encode("utf-8"))
    password_ok = hmac.compare_digest(password.encode("utf-8"), configured_password.encode("utf-8"))
    return user_ok and password_ok


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def create_session(username: str) -> tuple[str, str]:
    _, _, secret = auth_settings()
    csrf = secrets.token_urlsafe(32)
    payload = json.dumps({"sub": username, "csrf": csrf, "exp": int(time.time()) + SESSION_SECONDS}, separators=(",", ":")).encode()
    encoded = _encode(payload)
    signature = _encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
    return encoded + "." + signature, csrf


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        encoded, signature = token.rsplit(".", 1)
        _, _, secret = auth_settings()
        expected = _encode(hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        configured_username, _, _ = auth_settings()
        if payload.get("sub") != configured_username or int(payload.get("exp", 0)) <= int(time.time()):
            return None
        if not isinstance(payload.get("csrf"), str) or len(payload["csrf"]) < 24:
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, RuntimeError, binascii.Error):
        return None
