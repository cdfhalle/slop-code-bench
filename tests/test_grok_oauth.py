"""SuperGrok device login and the local OpenAI proxy. No network."""

# ruff: noqa: S105

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scb_slurm.grok_oauth import GrokAuthError  # noqa: E402
from scb_slurm.grok_oauth import Tokens  # noqa: E402
from scb_slurm.grok_oauth import _post_form  # noqa: E402
from scb_slurm.grok_oauth import load_tokens  # noqa: E402
from scb_slurm.grok_oauth import poll_device_token  # noqa: E402
from scb_slurm.grok_oauth import refresh_tokens  # noqa: E402
from scb_slurm.grok_oauth import request_device_code  # noqa: E402
from scb_slurm.grok_oauth import save_tokens  # noqa: E402
from scb_slurm.grok_oauth import write_secret  # noqa: E402
from scb_slurm.grok_proxy import GrokProxy  # noqa: E402
from scb_slurm.grok_proxy import advertise_host  # noqa: E402
from scb_slurm.grok_proxy import presented_key  # noqa: E402
from scb_slurm.grok_proxy import serve  # noqa: E402
from slop_code.common.llms import ModelDefinition  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_write_secret_is_user_only(tmp_path: Path) -> None:
    path = tmp_path / "dir" / "auth.json"
    write_secret(path, "token\n")
    assert path.read_text(encoding="utf-8") == "token\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_token_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCB_GROK_CONFIG_DIR", str(tmp_path))
    save_tokens(Tokens("access", "refresh", 123.0))
    loaded = load_tokens()
    assert loaded.access_token == "access"
    assert loaded.refresh_token == "refresh"
    assert (tmp_path / "auth.json").stat().st_mode & 0o777 == 0o600


def test_post_form_rejects_non_xai_url() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    with pytest.raises(GrokAuthError, match="x.ai"):
        _post_form(client, "https://example.com/oauth2/token", {"a": "b"})


def test_device_code_requires_fields() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"device_code": "d"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GrokAuthError, match="missing"):
        request_device_code(client)


def test_poll_waits_through_pending_then_returns_tokens() -> None:
    clock = _Clock()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = poll_device_token(
        client,
        device_code="device",
        expires_in=30,
        interval=5,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert tokens.access_token == "access"
    assert tokens.refresh_token == "refresh"
    assert clock.t == 10


def test_poll_backs_off_on_slow_down() -> None:
    clock = _Clock()
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(
            200,
            json={
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 10,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    poll_device_token(
        client,
        device_code="device",
        expires_in=60,
        interval=5,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert clock.t == 15


def test_poll_stops_on_access_denied() -> None:
    clock = _Clock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "access_denied"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GrokAuthError, match="access_denied"):
        poll_device_token(
            client,
            device_code="device",
            expires_in=30,
            interval=1,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )


def test_refresh_stores_rotated_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "refresh_token=old-refresh" in body
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 120,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = refresh_tokens(client, "old-refresh", now=1_000.0)
    assert tokens.access_token == "new-access"
    assert tokens.refresh_token == "new-refresh"
    assert tokens.expires_at == 1_120.0


def test_refresh_keeps_old_token_when_response_omits_it() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": "new-access", "expires_in": 60},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = refresh_tokens(client, "keep-me", now=50.0)
    assert tokens.refresh_token == "keep-me"


def test_store_refreshes_near_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCB_GROK_CONFIG_DIR", str(tmp_path))
    save_tokens(Tokens("old", "refresh-1", time.time() + 10))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "new",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from scb_slurm.grok_oauth import TokenStore

    assert TokenStore(client=client).bearer() == "new"
    assert load_tokens().refresh_token == "refresh-2"


def test_store_skips_refresh_when_token_is_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SCB_GROK_CONFIG_DIR", str(tmp_path))
    save_tokens(Tokens("still-good", "refresh-1", time.time() + 3600))

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("refresh should not run")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    from scb_slurm.grok_oauth import TokenStore

    assert TokenStore(client=client).bearer() == "still-good"


class _Upstream(BaseHTTPRequestHandler):
    hits: list[dict[str, object]] = []
    fail_first: bool = False

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or "0")
        body = self.rfile.read(length)
        self.hits.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )
        if self.fail_first and len(self.hits) == 1:
            payload = b'{"error":"unauthorized"}'
            status = 401
        else:
            payload = b'{"id":"chatcmpl","choices":[]}'
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        _ = (fmt, args)


def _start(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_proxy_forwards_and_replaces_the_client_key(tmp_path: Path) -> None:
    _Upstream.hits = []
    _Upstream.fail_first = False
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    upstream_thread = _start(upstream)
    bearer_calls: list[bool] = []

    def bearer(*, force: bool = False) -> str:
        bearer_calls.append(force)
        return "oauth-access"

    client = httpx.Client(timeout=10.0)
    host, port = upstream.server_address[:2]
    proxy = serve(
        host="127.0.0.1",
        port=0,
        upstream=f"http://{host}:{port}/v1",
        client=client,
        bearer=bearer,
        admin_key="proxy-key",
        endpoint=tmp_path / "endpoint.json",
    )
    proxy_thread = _start(proxy)
    try:
        proxy_port = proxy.server_address[1]
        response = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-key"},
            json={"model": "grok-4.7", "messages": [{"role": "user", "content": "hi"}]},
            timeout=10.0,
        )
        assert response.status_code == 200
        assert response.json()["id"] == "chatcmpl"
        assert _Upstream.hits[0]["path"] == "/v1/chat/completions"
        assert _Upstream.hits[0]["auth"] == "Bearer oauth-access"
        sent = json.loads(_Upstream.hits[0]["body"])  # type: ignore[arg-type]
        assert sent["model"] == "grok-4.7"
        assert bearer_calls == [False]
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
        client.close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)


def test_proxy_retries_once_when_upstream_says_401(tmp_path: Path) -> None:
    _Upstream.hits = []
    _Upstream.fail_first = True
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    _start(upstream)

    def bearer(*, force: bool = False) -> str:
        return "refreshed" if force else "stale"

    client = httpx.Client(timeout=10.0)
    host, port = upstream.server_address[:2]
    proxy = serve(
        host="127.0.0.1",
        port=0,
        upstream=f"http://{host}:{port}/v1",
        client=client,
        bearer=bearer,
        admin_key="proxy-key",
        endpoint=tmp_path / "endpoint.json",
    )
    _start(proxy)
    try:
        proxy_port = proxy.server_address[1]
        response = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            headers={"Authorization": "Bearer proxy-key"},
            json={"model": "grok-4.7"},
            timeout=10.0,
        )
        assert response.status_code == 200
        assert [hit["auth"] for hit in _Upstream.hits] == [
            "Bearer stale",
            "Bearer refreshed",
        ]
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
        client.close()


def test_proxy_rejects_a_missing_or_wrong_key(tmp_path: Path) -> None:
    client = httpx.Client(timeout=10.0)
    proxy = serve(
        host="127.0.0.1",
        port=0,
        upstream="http://127.0.0.1:9",
        client=client,
        bearer=lambda *, force=False: "unused",  # noqa: ARG005
        admin_key="proxy-key",
        endpoint=tmp_path / "endpoint.json",
    )
    _start(proxy)
    try:
        base = f"http://127.0.0.1:{proxy.server_address[1]}"
        health = httpx.get(f"{base}/health", timeout=5.0)
        assert health.status_code == 200
        missing = httpx.get(f"{base}/v1/models", timeout=5.0)
        assert missing.status_code == 401
        wrong = httpx.get(
            f"{base}/v1/models",
            headers={"Authorization": "Bearer no"},
            timeout=5.0,
        )
        assert wrong.status_code == 401
        unknown = httpx.get(
            f"{base}/v1/nope",
            headers={"Authorization": "Bearer proxy-key"},
            timeout=5.0,
        )
        assert unknown.status_code == 404
    finally:
        proxy.shutdown()
        proxy.server_close()
        client.close()


def test_advertise_host_hides_the_wildcard() -> None:
    wildcard = ".".join(["0", "0", "0", "0"])
    assert advertise_host("127.0.0.1") == "127.0.0.1"
    assert advertise_host(wildcard) != wildcard


def test_presented_key_reads_bearer_or_header() -> None:
    assert presented_key({"Authorization": "Bearer abc"}) == "abc"
    assert presented_key({"X-API-Key": "xyz"}) == "xyz"
    assert presented_key({}) == ""


def test_grok_model_uses_litellm_without_a_baked_base_url() -> None:
    path = Path("configs/models/grok-4.7.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    model = ModelDefinition.model_validate(data)
    settings = model.agent_specific["mini_swe"]
    assert model.provider == "openai"
    assert model.internal_name == "grok-4.7"
    assert settings["model_name"] == "openai/grok-4.7"
    assert settings["model_class"] == "litellm"
    assert "api_base" not in settings
    assert model.pricing.input == 0


def test_proxy_class_is_constructed_with_the_admin_key() -> None:
    proxy = GrokProxy(
        upstream="https://api.x.ai/v1",
        admin_key="k",
        bearer=lambda *, force=False: "t",  # noqa: ARG005
        client=httpx.Client(),
    )
    assert proxy.authorized({"Authorization": "Bearer k"})
    assert not proxy.authorized({"Authorization": "Bearer other"})
    proxy.client.close()
