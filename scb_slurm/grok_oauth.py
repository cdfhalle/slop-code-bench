"""SuperGrok login for the Slurm harness.

OpenCode and Hermes Agent sign in to a SuperGrok or X Premium+ subscription
with the public Grok CLI OAuth client (device code against auth.x.ai, then
Bearer calls to api.x.ai). This module does that login and keeps one
refresh lock. Parallel array tasks must not refresh on their own: xAI
rotates the refresh token, and two refreshes at once invalidate each other.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

# Public client id shipped with the Grok CLI. Not a secret. OpenCode and
# Hermes both send this id; auth.x.ai rejects a private loopback client.
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
OAUTH_EXCHANGE_URL = "https://auth.x.ai/oauth2/token"
API_BASE = "https://api.x.ai/v1"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
# Access tokens are short (about an hour, sometimes longer). Refresh early
# so a completion that starts just before expiry still gets a live bearer.
REFRESH_SKEW_S = 300
_FORM = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json",
}


class GrokAuthError(RuntimeError):
    """Login, refresh, or the on-disk token file failed."""


@dataclass(frozen=True)
class Tokens:
    """OAuth token pair. expires_at is unix seconds."""

    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"  # noqa: S105

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Tokens:
        try:
            access = str(payload["access_token"])
            refresh = str(payload["refresh_token"])
            expires_at = float(payload["expires_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GrokAuthError(f"auth file is missing fields: {exc}") from exc
        if not access or not refresh:
            raise GrokAuthError("auth file has an empty token")
        token_type = str(payload.get("token_type") or "Bearer")
        return cls(access, refresh, expires_at, token_type)


def config_dir() -> Path:
    """Token directory. Override with SCB_GROK_CONFIG_DIR in tests."""
    override = os.environ.get("SCB_GROK_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "scb-grok-oauth"


def auth_path() -> Path:
    return config_dir() / "auth.json"


def proxy_key_path() -> Path:
    return config_dir() / "proxy.key"


def endpoint_path() -> Path:
    return config_dir() / "endpoint.json"


def write_secret(path: Path, text: str) -> None:
    """Write text mode 0600. The directory is 0700. Replace is atomic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    # O_NOFOLLOW is not on every filesystem; the mode on open still beats
    # a world-readable umask window between write_text and chmod.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    tmp.chmod(0o600)
    tmp.replace(path)
    path.chmod(0o600)


def load_or_create_proxy_key() -> str:
    """Key the local proxy requires. Separate from the xAI access token."""
    path = proxy_key_path()
    if path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(32)
    write_secret(path, key + "\n")
    return key


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _check_https_xai(url: str, *, what: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        host == "x.ai" or host.endswith(".x.ai")
    ):
        raise GrokAuthError(f"{what} is not an https x.ai URL: {url}")
    return url


def _post_form(
    client: httpx.Client, url: str, data: dict[str, str]
) -> httpx.Response:
    _check_https_xai(url, what="token URL")
    try:
        return client.post(url, data=data, headers=_FORM)
    except httpx.HTTPError as exc:
        raise GrokAuthError(f"request to {url} failed: {exc}") from exc


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise GrokAuthError(
            f"xAI returned a non-JSON body (HTTP {response.status_code})"
        ) from exc
    if not isinstance(payload, dict):
        raise GrokAuthError(
            f"xAI returned {type(payload).__name__}, expected an object"
        )
    return payload


def _tokens_from_response(payload: dict[str, Any], *, now: float) -> Tokens:
    access = str(payload.get("access_token") or "")
    refresh = str(payload.get("refresh_token") or "")
    if not access or not refresh:
        raise GrokAuthError(
            "xAI token response was missing access_token or refresh_token"
        )
    try:
        expires_in = int(payload.get("expires_in") or 0)
    except (TypeError, ValueError) as exc:
        raise GrokAuthError("xAI token response has a bad expires_in") from exc
    if expires_in <= 0:
        raise GrokAuthError("xAI token response has no positive expires_in")
    token_type = str(payload.get("token_type") or "Bearer")
    return Tokens(access, refresh, now + expires_in, token_type)


def request_device_code(client: httpx.Client) -> dict[str, Any]:
    response = _post_form(
        client,
        DEVICE_CODE_URL,
        {"client_id": CLIENT_ID, "scope": SCOPE},
    )
    payload = _json_object(response)
    if response.status_code != 200:
        raise GrokAuthError(
            "xAI device-code request failed "
            f"(HTTP {response.status_code}): {payload.get('error')}"
        )
    required = (
        "device_code",
        "user_code",
        "verification_uri",
        "expires_in",
        "interval",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise GrokAuthError(
            "xAI device-code response missing " + ", ".join(missing)
        )
    return payload


def poll_device_token(
    client: httpx.Client,
    *,
    device_code: str,
    expires_in: int,
    interval: int,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> Tokens:
    """Poll until the user approves, denies, or the code expires."""
    deadline = monotonic() + max(1, int(expires_in))
    wait_s = max(1, int(interval))
    while monotonic() < deadline:
        sleep(wait_s)
        response = _post_form(
            client,
            OAUTH_EXCHANGE_URL,
            {
                "grant_type": DEVICE_GRANT,
                "client_id": CLIENT_ID,
                "device_code": device_code,
            },
        )
        payload = _json_object(response)
        if response.status_code == 200 and payload.get("access_token"):
            return _tokens_from_response(payload, now=time.time())
        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            wait_s += 5
            continue
        if error in {"access_denied", "expired_token"}:
            raise GrokAuthError(f"xAI device login {error}")
        raise GrokAuthError(
            "xAI device login failed "
            f"(HTTP {response.status_code}): {error or payload}"
        )
    raise GrokAuthError("timed out waiting for xAI device approval")


def refresh_tokens(
    client: httpx.Client, refresh_token: str, *, now: float | None = None
) -> Tokens:
    response = _post_form(
        client,
        OAUTH_EXCHANGE_URL,
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )
    payload = _json_object(response)
    if response.status_code != 200:
        raise GrokAuthError(
            "xAI refresh failed "
            f"(HTTP {response.status_code}): {payload.get('error')}"
        )
    # A response that omits refresh_token keeps the one we sent. xAI usually
    # rotates it; when it does, the new value must be what we store.
    if not payload.get("refresh_token"):
        payload = {**payload, "refresh_token": refresh_token}
    return _tokens_from_response(payload, now=time.time() if now is None else now)


def save_tokens(tokens: Tokens) -> None:
    write_secret(
        auth_path(),
        json.dumps(tokens.to_json(), indent=2) + "\n",
    )


def load_tokens() -> Tokens:
    path = auth_path()
    if not path.is_file():
        raise GrokAuthError(
            f"not logged in ({path}). Run: python -m scb_slurm.grok_proxy login"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GrokAuthError(f"{path} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise GrokAuthError(f"{path} must contain a JSON object")
    return Tokens.from_json(payload)


class TokenStore:
    """Load, refresh, and save tokens. Refresh is serialized on a file lock."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def bearer(self, *, force: bool = False) -> str:
        with _lock(auth_path()):
            tokens = load_tokens()
            if not force and tokens.expires_at - time.time() > REFRESH_SKEW_S:
                return tokens.access_token
            client = self._client or httpx.Client(timeout=30.0)
            close = self._client is None
            try:
                refreshed = refresh_tokens(client, tokens.refresh_token)
            finally:
                if close:
                    client.close()
            save_tokens(refreshed)
            return refreshed.access_token


def login(*, open_wait: Any = time.sleep) -> Tokens:
    """Device-code login. Prints the URL. Does not open a browser.

    The cluster session has no display. Approve the URL on any machine.
    """
    timeout = httpx.Timeout(30.0)
    with httpx.Client(timeout=timeout) as client:
        device = request_device_code(client)
        url = str(
            device.get("verification_uri_complete")
            or device["verification_uri"]
        )
        code = str(device["user_code"])
        print(
            "Sign in with the SuperGrok account that should pay for this run.",
            flush=True,
        )
        print(f"Open: {url}", flush=True)
        print(f"Code: {code}", flush=True)
        print(
            "Waiting for approval "
            f"(code expires in {int(device['expires_in'])}s)...",
            flush=True,
        )
        tokens = poll_device_token(
            client,
            device_code=str(device["device_code"]),
            expires_in=int(device["expires_in"]),
            interval=int(device["interval"]),
            sleep=open_wait,
        )
    save_tokens(tokens)
    print(f"Saved tokens to {auth_path()}", flush=True)
    return tokens
