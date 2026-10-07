"""Long-lived enroot runtime. One supervisor, many streamed commands."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from slop_code.execution.assets import ResolvedStaticAsset
from slop_code.execution.enroot_runtime.models import EnrootEnvironmentSpec
from slop_code.execution.enroot_runtime.session import EnrootSession
from slop_code.execution.models import EnvironmentSpec
from slop_code.execution.protocols import StreamingRuntime
from slop_code.execution.runtime import RuntimeEvent
from slop_code.execution.runtime import RuntimeResult
from slop_code.execution.runtime import SolutionRuntimeError
from slop_code.execution.shared import HANDLE_ENTRY_NAME
from slop_code.execution.shared import split_setup_output
from slop_code.execution.shared import write_entry_script


class EnrootStreamingRuntime(StreamingRuntime):
    """Stand-in for ``docker exec`` against one container kept alive."""

    def __init__(
        self,
        spec: EnrootEnvironmentSpec,
        working_dir: Path,
        static_assets: dict[str, ResolvedStaticAsset],
        *,
        is_evaluation: bool,
        mounts: dict[str, dict[str, str] | str],
        env_vars: dict[str, str],
        setup_command: str | None,
        disable_setup: bool,
        image: str | None,
        attach_slirp: bool | None,
    ) -> None:
        self.spec = spec
        self.cwd = working_dir
        self._is_evaluation = is_evaluation
        self._env_vars = dict(env_vars)
        self._setup_command = setup_command
        self._disable_setup = disable_setup
        self._exit_code: int | None = None
        self._busy = False
        self._session = EnrootSession(
            spec,
            working_dir,
            static_assets,
            mounts,
            image=image,
            attach_slirp=attach_slirp,
            remove_on_close=True,
        )
        self._session.open(once=False, container_env=self._base_env({}))

    def _base_env(self, env: dict[str, str]) -> dict[str, str]:
        merged = dict(self._env_vars)
        merged.update(env)
        return self.spec.get_full_env(merged)

    def _setup_commands(self) -> list[str]:
        commands = list(
            self.spec.get_setup_commands(is_evaluation=self._is_evaluation)
        )
        if self._setup_command:
            commands.append(self._setup_command)
        return commands

    def _prepare(self, command: str) -> str:
        if self._disable_setup:
            return command
        write_entry_script(self.cwd, command, self._setup_commands())
        return f"bash -l {HANDLE_ENTRY_NAME}"

    def stream(
        self,
        command: str,
        env: dict[str, str],
        timeout: float | None,
    ) -> Iterator[RuntimeEvent]:
        prepared = self._prepare(command)
        started = time.monotonic()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 1
        timed_out = False
        self._busy = True
        self._exit_code = None
        try:
            for event in self._session.request(
                prepared,
                self._base_env(env),
                cwd=self.spec.enroot.workdir,
                timeout=timeout,
                stdin=None,
            ):
                op = event.get("op")
                if op == "stdout":
                    text = str(event.get("text", ""))
                    stdout_parts.append(text)
                    yield RuntimeEvent(kind="stdout", text=text)
                elif op == "stderr":
                    text = str(event.get("text", ""))
                    stderr_parts.append(text)
                    yield RuntimeEvent(kind="stderr", text=text)
                elif op == "done":
                    exit_code = int(event.get("exit_code", 1))
                    timed_out = bool(event.get("timed_out", False))
        finally:
            self._busy = False
            self._exit_code = exit_code
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        if self._disable_setup:
            setup_stdout, setup_stderr = "", ""
        else:
            setup_stdout, stdout, setup_stderr, stderr = split_setup_output(
                stdout, stderr
            )
        yield RuntimeEvent(
            kind="finished",
            result=RuntimeResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                setup_stdout=setup_stdout,
                setup_stderr=setup_stderr,
                elapsed=time.monotonic() - started,
                timed_out=timed_out,
            ),
        )

    def poll(self) -> int | None:
        if self._busy:
            return None
        return self._exit_code

    def kill(self) -> None:
        self._session.close()
        self._busy = False

    def cleanup(self) -> None:
        self._session.close()

    @classmethod
    def spawn(
        cls,
        environment: EnvironmentSpec,
        working_dir: Path,
        static_assets: dict[str, ResolvedStaticAsset] | None = None,
        ports: dict[int, int] | None = None,
        mounts: dict[str, dict[str, str] | str] | None = None,
        env_vars: dict[str, str] | None = None,
        setup_command: str | None = None,
        *,
        is_evaluation: bool = False,
        disable_setup: bool = False,
        **runtime_kwargs: object,
    ) -> EnrootStreamingRuntime:
        del ports
        if not isinstance(environment, EnrootEnvironmentSpec):
            raise SolutionRuntimeError(
                f"enroot streaming runtime got {environment.type}"
            )
        image = runtime_kwargs.get("image")
        attach = runtime_kwargs.get("attach_slirp")
        return cls(
            environment,
            working_dir,
            static_assets or {},
            is_evaluation=is_evaluation,
            mounts=mounts or {},
            env_vars=env_vars or {},
            setup_command=setup_command,
            disable_setup=disable_setup,
            image=image if isinstance(image, str) and image else None,
            attach_slirp=attach if isinstance(attach, bool) else None,
        )
