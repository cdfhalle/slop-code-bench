"""Host side of one enroot container and its supervisor socket."""

from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from slop_code.execution.assets import ResolvedStaticAsset
from slop_code.execution.enroot_runtime.backend import build_mounts
from slop_code.execution.enroot_runtime.backend import container_name_for
from slop_code.execution.enroot_runtime.backend import enroot_subprocess_env
from slop_code.execution.enroot_runtime.backend import ensure_created
from slop_code.execution.enroot_runtime.backend import host_socket
from slop_code.execution.enroot_runtime.backend import install_supervisor
from slop_code.execution.enroot_runtime.backend import kill_process_group
from slop_code.execution.enroot_runtime.backend import remove_container
from slop_code.execution.enroot_runtime.backend import start_argv
from slop_code.execution.enroot_runtime.models import EnrootEnvironmentSpec
from slop_code.execution.runtime import SolutionRuntimeError

_CONNECT_TIMEOUT = 10.0


class EnrootSession:
    """One unpacked container and, while a command is in flight, one start."""

    def __init__(
        self,
        spec: EnrootEnvironmentSpec,
        working_dir: Path,
        static_assets: dict[str, ResolvedStaticAsset],
        runtime_mounts: dict[str, dict[str, str] | str],
        *,
        image: str | None = None,
        attach_slirp: bool | None = None,
        remove_on_close: bool = False,
    ) -> None:
        if not spec.enroot.mount_workspace:
            raise SolutionRuntimeError(
                "enroot runtime requires mount_workspace so the supervisor "
                "socket is visible on the host"
            )
        self.spec = spec
        self.working_dir = working_dir
        self.image = image or spec.enroot.image
        self.attach_slirp = (
            spec.enroot.attach_slirp if attach_slirp is None else attach_slirp
        )
        self.remove_on_close = remove_on_close
        self.name = container_name_for(working_dir)
        self.mounts = build_mounts(
            spec, working_dir, static_assets, runtime_mounts
        )
        self._proc: subprocess.Popen[bytes] | None = None
        self._slirp: subprocess.Popen[bytes] | None = None
        self._conn: socket.socket | None = None
        self._buf = b""
        ensure_created(spec, self.name, self.image)
        install_supervisor(spec, working_dir)
        if self.attach_slirp:
            resolv = working_dir / ".slop-enroot" / "resolv.conf"
            resolv.write_text("nameserver 10.0.2.3\n", encoding="utf-8")
            self.mounts.append(
                f"{resolv}:/etc/resolv.conf:none:x-create=file,bind,ro"
            )

    def open(self, *, once: bool, container_env: dict[str, str]) -> None:
        """Start the supervisor and, when asked, attach slirp4netns."""
        if self._proc is not None and self._proc.poll() is None:
            return
        argv = start_argv(
            self.spec,
            self.name,
            self.mounts,
            container_env,
            once=once,
        )
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - argv built above.
                argv,
                env=enroot_subprocess_env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise SolutionRuntimeError("failed to launch enroot start") from exc
        # The supervisor socket exists only after enroot has entered the
        # network namespace. slirp must attach to that namespace, not to the
        # host namespace the process still has during early startup.
        self._connect()
        if self.attach_slirp:
            self._attach_slirp()

    def _netns_pid(self) -> str:
        """A process id that is inside the container network namespace.

        ``enroot start`` can stay in the host netns and run the command in a
        child. slirp has to target that child, or it configures the host.
        """
        proc = self._proc
        if proc is None:
            raise SolutionRuntimeError("enroot is not running")
        host_net = Path("/proc/self/ns/net").stat().st_ino

        def walk(pid: int) -> list[int]:
            path = Path(f"/proc/{pid}/task/{pid}/children")
            try:
                text = path.read_text()
            except OSError:
                return []
            found: list[int] = []
            for part in text.split():
                child = int(part)
                found.append(child)
                found.extend(walk(child))
            return found

        chosen = proc.pid
        for candidate in walk(proc.pid):
            try:
                if Path(f"/proc/{candidate}/ns/net").stat().st_ino != host_net:
                    chosen = candidate
            except OSError:
                continue
        return str(chosen)

    def _attach_slirp(self) -> None:
        proc = self._proc
        if proc is None:
            return
        read_fd, write_fd = os.pipe()
        argv = [
            self.spec.enroot.slirp_binary,
            "--configure",
            f"--ready-fd={write_fd}",
            "--disable-host-loopback",
            self._netns_pid(),
            "tap0",
        ]
        try:
            self._slirp = subprocess.Popen(  # noqa: S603
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(write_fd,),
            )
        except OSError as exc:
            os.close(read_fd)
            os.close(write_fd)
            self.close()
            raise SolutionRuntimeError(
                f"failed to start {self.spec.enroot.slirp_binary}"
            ) from exc
        os.close(write_fd)
        deadline = time.monotonic() + 15
        ready = False
        while time.monotonic() < deadline:
            if self._slirp.poll() is not None:
                err = ""
                if self._slirp.stderr is not None:
                    err = self._slirp.stderr.read().decode("utf-8", errors="replace")
                os.close(read_fd)
                self.close()
                raise SolutionRuntimeError(f"slirp4netns exited: {err.strip()}")
            readable, _, _ = select.select([read_fd], [], [], 0.2)
            if readable:
                os.read(read_fd, 1)
                ready = True
                break
        os.close(read_fd)
        if not ready:
            self.close()
            raise SolutionRuntimeError("slirp4netns did not become ready")

    def _connect(self) -> None:
        sock_path = host_socket(self.working_dir)
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        last_error = ""
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                err = ""
                if proc.stderr is not None:
                    err = proc.stderr.read().decode("utf-8", errors="replace")
                raise SolutionRuntimeError(
                    f"enroot start exited before the supervisor was ready: {err}"
                )
            if sock_path.exists():
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    client.connect(str(sock_path))
                except OSError as exc:
                    last_error = str(exc)
                    client.close()
                    time.sleep(0.05)
                    continue
                self._conn = client
                self._buf = b""
                return
            time.sleep(0.05)
        self.close()
        raise SolutionRuntimeError(
            f"supervisor socket did not appear at {sock_path}: {last_error}"
        )

    def request(
        self,
        command: str,
        env: dict[str, str],
        *,
        cwd: str,
        timeout: float | None,
        stdin: str | None,
    ) -> Iterator[dict[str, Any]]:
        conn = self._conn
        if conn is None:
            raise SolutionRuntimeError("enroot session is not open")
        payload: dict[str, Any] = {
            "cmd": command,
            "env": env,
            "cwd": cwd,
            "timeout": timeout,
        }
        if stdin is not None:
            payload["stdin"] = stdin
        try:
            conn.sendall((json.dumps(payload) + "\n").encode())
        except OSError as exc:
            raise SolutionRuntimeError("failed to write supervisor request") from exc
        while True:
            event = self._read_event()
            yield event
            if event.get("op") == "done":
                return

    def _read_event(self) -> dict[str, Any]:
        conn = self._conn
        if conn is None:
            raise SolutionRuntimeError("enroot session is not open")
        while b"\n" not in self._buf:
            try:
                chunk = conn.recv(65536)
            except OSError as exc:
                raise SolutionRuntimeError(
                    "supervisor socket closed while reading"
                ) from exc
            if not chunk:
                raise SolutionRuntimeError("supervisor socket closed")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        data = json.loads(line)
        if not isinstance(data, dict):
            raise SolutionRuntimeError("supervisor sent a non-object event")
        return data

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        kill_process_group(self._slirp)
        self._slirp = None
        kill_process_group(self._proc)
        self._proc = None
        if self.remove_on_close:
            remove_container(self.spec, self.name)
