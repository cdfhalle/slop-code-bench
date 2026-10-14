"""Command supervisor that runs inside an enroot container.

Stdlib only. The image does not have this repo installed. The host copies
this file into the bind-mounted workspace and starts it with the image's
``python``. One accepted connection runs one command, then the socket is
free for the next command. ``--once`` exits after that command so a
one-shot ``enroot start`` tears the process namespace down.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any


def load_image_env() -> None:
    """Apply /etc/environment without overriding variables already set."""
    path = Path("/etc/environment")
    if not path.is_file():
        return
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def _send(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode())


def _read_request(conn: socket.socket) -> dict[str, Any]:
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            msg = "supervisor client closed before sending a request"
            raise ConnectionError(msg)
        buf += chunk
    line = buf.split(b"\n", 1)[0]
    data = json.loads(line)
    if not isinstance(data, dict) or "cmd" not in data:
        msg = "supervisor request must be a JSON object with 'cmd'"
        raise ValueError(msg)
    return data


def _stream_command(conn: socket.socket, request: dict[str, Any]) -> int:
    env = os.environ.copy()
    extra = request.get("env") or {}
    if isinstance(extra, dict):
        env.update({str(key): str(value) for key, value in extra.items()})
    cwd = str(request.get("cwd") or "/")
    timeout = request.get("timeout")
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    proc = subprocess.Popen(  # noqa: S603 - the command is the caller's.
        ["/bin/bash", "-lc", str(request["cmd"])],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if request.get("stdin") is not None else None,
        start_new_session=True,
    )
    if request.get("stdin") is not None and proc.stdin is not None:
        proc.stdin.write(str(request["stdin"]).encode())
        proc.stdin.close()
    stdout = proc.stdout
    stderr = proc.stderr
    if stdout is None or stderr is None:
        proc.kill()
        return 1
    selector = selectors.DefaultSelector()
    selector.register(stdout, selectors.EVENT_READ, "stdout")
    selector.register(stderr, selectors.EVENT_READ, "stderr")
    open_pipes = 2
    timed_out = False
    while open_pipes:
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                break
        wait = 0.2 if remaining is None else remaining
        events = selector.select(timeout=wait)
        if not events and proc.poll() is not None:
            events = selector.select(timeout=0)
            if not events:
                break
        for key, _mask in events:
            pipe: IO[bytes] = key.fileobj  # type: ignore[assignment]
            chunk = os.read(pipe.fileno(), 4096)
            if not chunk:
                selector.unregister(pipe)
                open_pipes -= 1
                continue
            text = chunk.decode("utf-8", errors="replace")
            _send(conn, {"op": key.data, "text": text})
    exit_code = proc.wait()
    _send(
        conn,
        {"op": "done", "exit_code": exit_code, "timed_out": timed_out},
    )
    return exit_code


def serve(sock_path: str, *, once: bool) -> None:
    load_image_env()
    path = Path(sock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        conn, _addr = server.accept()
        with conn:
            while True:
                try:
                    request = _read_request(conn)
                except ConnectionError:
                    return
                _stream_command(conn, request)
                if once:
                    return
    finally:
        server.close()
        path.unlink(missing_ok=True)


def main(argv: list[str]) -> None:
    once = "--once" in argv
    args = [arg for arg in argv if arg != "--once"]
    if len(args) != 1:
        sys.stderr.write("usage: container_supervisor.py [--once] SOCK_PATH\n")
        sys.exit(2)
    serve(args[0], once=once)


if __name__ == "__main__":
    main(sys.argv[1:])
