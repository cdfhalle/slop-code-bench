"""OpenAI-compatible proxy in front of a SuperGrok OAuth token.

mini-swe-agent speaks to an API. This process is the only thing that holds
the OAuth refresh. Point submit_batch --api-base at the printed URL.

    python -m scb_slurm.grok_proxy login
    sbatch slurm/grok_proxy.sbatch
    export SCB_GROK_PROXY_KEY_FILE=$HOME/.config/scb-grok-oauth/proxy.key
    python -m scb_slurm.submit_batch --model openai/grok-4.7 \\
        --api-base "$(python -m scb_slurm.grok_proxy api-base)" \\
        --problems <name> --throttle 1 --run grok-<name>
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import socket
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import httpx

from scb_slurm.grok_oauth import API_BASE
from scb_slurm.grok_oauth import GrokAuthError
from scb_slurm.grok_oauth import TokenStore
from scb_slurm.grok_oauth import endpoint_path
from scb_slurm.grok_oauth import load_or_create_proxy_key
from scb_slurm.grok_oauth import login
from scb_slurm.grok_oauth import proxy_key_path

log = logging.getLogger("scb_grok_proxy")

# Paths Hermes forwards for the same OAuth token. mini-swe uses chat
# completions. /responses is there for a client that speaks that API.
_ALLOWED = frozenset(
    {
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/completions",
        "/v1/models",
        "/v1/embeddings",
    }
)
_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }
)
# Copied onto the upstream request. Authorization is replaced, not copied.
_FORWARD = frozenset({"content-type", "accept"})

BearerFunc = Callable[..., str]


def _constant_time_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def presented_key(headers: Any) -> str:
    """Bearer token or X-API-Key. Empty when the client sent neither."""
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (headers.get("X-API-Key") or headers.get("x-api-key") or "").strip()


def advertise_host(bind_host: str) -> str:
    """Hostname other Slurm nodes can resolve. Never advertise a wildcard."""
    if bind_host in {"0.0.0.0", "::", ""}:  # noqa: S104
        return socket.gethostname()
    return bind_host


def write_endpoint(api_base: str, path: Any | None = None) -> Any:
    dest = endpoint_path() if path is None else path
    payload = {
        "api_base": api_base,
        "proxy_key_file": str(proxy_key_path()),
        "upstream": API_BASE,
    }
    # The key itself stays in proxy.key. This file only points at it.
    text = json.dumps(payload, indent=2) + "\n"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


class GrokProxy:
    """Forward a few /v1 routes. Inject a fresh OAuth bearer on each call."""

    def __init__(
        self,
        *,
        upstream: str,
        admin_key: str,
        bearer: BearerFunc,
        client: httpx.Client,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.admin_key = admin_key
        self.bearer = bearer
        self.client = client

    def _upstream_url(self, path: str) -> str:
        """Join base and path without a doubled /v1.

        Clients call ``/v1/chat/completions``. The xAI base already ends in
        ``/v1``, and ``base + path`` would request ``/v1/v1/...``.
        """
        base = self.upstream.rstrip("/")
        if base.endswith("/v1") and path.startswith("/v1/"):
            return base[: -len("/v1")] + path
        return base + path

    def authorized(self, headers: Any) -> bool:
        key = presented_key(headers)
        if not key:
            return False
        return _constant_time_eq(key, self.admin_key)

    def open_upstream(
        self,
        method: str,
        path: str,
        headers: Any,
        body: bytes,
        *,
        token: str,
    ) -> httpx.Response:
        upstream_headers = {
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "identity",
        }
        for name in _FORWARD:
            value = headers.get(name)
            if value:
                upstream_headers[name] = value
        request = self.client.build_request(
            method,
            self._upstream_url(path),
            headers=upstream_headers,
            content=body if body else None,
        )
        return self.client.send(request, stream=True)


def _chunk(data: bytes) -> bytes:
    return f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ProxyServer

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        if size < 0:
            return b""
        return self.rfile.read(size)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if self.command == "GET" and path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        proxy = self.server.proxy
        if not proxy.authorized(self.headers):
            self._send_json(401, {"error": "proxy key required"})
            return
        if path not in _ALLOWED:
            self._send_json(404, {"error": f"path not forwarded: {path}"})
            return
        body = self._read_body() if self.command == "POST" else b""
        self._forward(path, body)

    def _forward(self, path: str, body: bytes) -> None:
        proxy = self.server.proxy
        response: httpx.Response | None = None
        try:
            token = proxy.bearer(force=False)
            response = proxy.open_upstream(
                self.command, path, self.headers, body, token=token
            )
            if response.status_code == 401:
                response.read()
                response.close()
                response = None
                token = proxy.bearer(force=True)
                response = proxy.open_upstream(
                    self.command, path, self.headers, body, token=token
                )
            self._write_upstream(response)
        except GrokAuthError as exc:
            if response is not None:
                response.close()
            self._send_json(401, {"error": str(exc)})
        except httpx.HTTPError as exc:
            if response is not None:
                response.close()
            log.warning("upstream request failed: %s", exc)
            self._send_json(502, {"error": "upstream request failed"})
        except (BrokenPipeError, ConnectionResetError):
            if response is not None:
                response.close()

    def _write_upstream(self, response: httpx.Response) -> None:
        try:
            self.send_response(response.status_code)
            for key, value in response.headers.multi_items():
                if key.lower() in _HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in response.iter_bytes():
                if chunk:
                    self.wfile.write(_chunk(chunk))
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        finally:
            response.close()


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        proxy: GrokProxy,
    ) -> None:
        self.proxy = proxy
        super().__init__(address, _Handler)


def serve(
    *,
    host: str,
    port: int,
    upstream: str = API_BASE,
    client: httpx.Client | None = None,
    bearer: BearerFunc | None = None,
    admin_key: str | None = None,
    endpoint: Any | None = None,
) -> ProxyServer:
    """Bind and return. The caller runs serve_forever.

    A missing client or bearer is filled from the on-disk login. Tests pass
    both so this does not touch the real token file.
    """
    http = client or httpx.Client(
        timeout=httpx.Timeout(
            connect=30.0, read=1800.0, write=120.0, pool=30.0
        ),
        follow_redirects=False,
    )
    if bearer is None:
        bearer = TokenStore(client=http).bearer
    if admin_key is None:
        admin_key = load_or_create_proxy_key()
    proxy = GrokProxy(
        upstream=upstream,
        admin_key=admin_key,
        bearer=bearer,
        client=http,
    )
    server = ProxyServer((host, port), proxy)
    actual = int(server.server_address[1])
    api_base = f"http://{advertise_host(host)}:{actual}/v1"
    dest = write_endpoint(api_base, endpoint)
    print(f"api_base={api_base}", flush=True)
    print(f"proxy_key_file={proxy_key_path()}", flush=True)
    print(f"endpoint_file={dest}", flush=True)
    return server


def _read_api_base() -> str:
    path = endpoint_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"no endpoint file at {path}: {exc}") from exc
    api_base = str(payload.get("api_base") or "")
    if not api_base:
        raise SystemExit(f"{path} has no api_base")
    print(api_base)
    return api_base


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="scb_slurm.grok_proxy")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="device-code SuperGrok login")
    sub.add_parser("api-base", help="print the running proxy base URL")

    serve_p = sub.add_parser("serve", help="forward /v1 to api.x.ai")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8766)
    serve_p.add_argument("--upstream", default=API_BASE)

    models_p = sub.add_parser("models", help="list model ids for this login")
    models_p.add_argument("--upstream", default=API_BASE)

    args = parser.parse_args(argv)
    if args.cmd == "login":
        login()
        return
    if args.cmd == "api-base":
        _read_api_base()
        return
    if args.cmd == "models":
        _print_models(args.upstream)
        return
    if args.cmd == "serve":
        server = serve(host=args.host, port=args.port, upstream=args.upstream)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        finally:
            server.server_close()
            server.proxy.client.close()
        return
    raise SystemExit(f"unknown command {args.cmd}")


def _print_models(upstream: str) -> None:
    store = TokenStore()
    token = store.bearer(force=False)
    url = upstream.rstrip("/") + "/models"
    with httpx.Client(timeout=60.0) as client:
        response = client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
    if response.status_code == 401:
        token = store.bearer(force=True)
        with httpx.Client(timeout=60.0) as client:
            response = client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
    if response.status_code != 200:
        raise SystemExit(
            f"GET {url} failed (HTTP {response.status_code}): {response.text[:500]}"
        )
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        print(response.text)
        return
    for item in data:
        if isinstance(item, dict) and item.get("id"):
            print(item["id"])


if __name__ == "__main__":
    main()
