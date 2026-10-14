"""One-shot enroot runtime. One command, then the start exits."""

from __future__ import annotations

import time
from pathlib import Path

from slop_code.execution.assets import ResolvedStaticAsset
from slop_code.execution.enroot_runtime.models import EnrootEnvironmentSpec
from slop_code.execution.enroot_runtime.session import EnrootSession
from slop_code.execution.models import EnvironmentSpec
from slop_code.execution.protocols import ExecRuntime
from slop_code.execution.runtime import RuntimeResult
from slop_code.execution.runtime import SolutionRuntimeError
from slop_code.execution.shared import HANDLE_ENTRY_NAME
from slop_code.execution.shared import split_setup_output
from slop_code.execution.shared import write_entry_script


class EnrootExecRuntime(ExecRuntime):
    """One command inside a fresh ``enroot start``.

    The unpacked rootfs is kept so the next exec against the same workspace
    does not pay ``unsquashfs`` again. ``cleanup`` stops the start and does
    not ``enroot remove``; node-local scratch goes away with the Slurm job.
    Streaming runtimes do remove their container.
    """

    def __init__(
        self,
        spec: EnrootEnvironmentSpec,
        working_dir: Path,
        command: str,
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
        self._command = command
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
            remove_on_close=False,
        )

    def _merged_env(self, env: dict[str, str]) -> dict[str, str]:
        merged = dict(self._env_vars)
        merged.update(env)
        return self.spec.get_full_env(merged)

    def _prepare(self) -> str:
        if self._disable_setup:
            return self._command
        commands = list(
            self.spec.get_setup_commands(is_evaluation=self._is_evaluation)
        )
        if self._setup_command:
            commands.append(self._setup_command)
        write_entry_script(self.cwd, self._command, commands)
        return f"bash -l {HANDLE_ENTRY_NAME}"

    def execute(
        self,
        env: dict[str, str],
        stdin: str | list[str] | None,
        timeout: float | None,
    ) -> RuntimeResult:
        stdin_text = "\n".join(stdin) if isinstance(stdin, list) else stdin
        self._session.open(
            once=True,
            container_env=self._merged_env({}),
        )
        started = time.monotonic()
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        exit_code = 1
        timed_out = False
        self._busy = True
        try:
            for event in self._session.request(
                self._prepare(),
                self._merged_env(env),
                cwd=self.spec.enroot.workdir,
                timeout=timeout,
                stdin=stdin_text,
            ):
                op = event.get("op")
                if op == "stdout":
                    stdout_parts.append(str(event.get("text", "")))
                elif op == "stderr":
                    stderr_parts.append(str(event.get("text", "")))
                elif op == "done":
                    exit_code = int(event.get("exit_code", 1))
                    timed_out = bool(event.get("timed_out", False))
        finally:
            self._busy = False
            self._exit_code = exit_code
            self._session.close()
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        if self._disable_setup:
            setup_stdout, setup_stderr = "", ""
        else:
            setup_stdout, stdout, setup_stderr, stderr = split_setup_output(
                stdout, stderr
            )
        return RuntimeResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            setup_stdout=setup_stdout,
            setup_stderr=setup_stderr,
            elapsed=time.monotonic() - started,
            timed_out=timed_out,
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
        command: str,
        static_assets: dict[str, ResolvedStaticAsset] | None = None,
        ports: dict[int, int] | None = None,
        mounts: dict[str, dict[str, str] | str] | None = None,
        env_vars: dict[str, str] | None = None,
        setup_command: str | None = None,
        *,
        is_evaluation: bool = False,
        disable_setup: bool = False,
        **runtime_kwargs: object,
    ) -> EnrootExecRuntime:
        del ports
        if not isinstance(environment, EnrootEnvironmentSpec):
            raise SolutionRuntimeError(
                f"enroot exec runtime got {environment.type}"
            )
        image = runtime_kwargs.get("image")
        attach = runtime_kwargs.get("attach_slirp")
        return cls(
            environment,
            working_dir,
            command,
            static_assets or {},
            is_evaluation=is_evaluation,
            mounts=mounts or {},
            env_vars=env_vars or {},
            setup_command=setup_command,
            disable_setup=disable_setup,
            image=image if isinstance(image, str) and image else None,
            attach_slirp=attach if isinstance(attach, bool) else None,
        )
