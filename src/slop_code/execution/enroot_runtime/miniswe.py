"""Per-command enroot environment for the MiniSWE agent.

Each action is a new ``enroot start``. The shell does not remember ``cd``
or exported variables. Files survive because the workspace is bind-mounted.
The host home directory is not a mount, and there is no switch to turn
that mount on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from slop_code.execution.enroot_runtime.backend import container_name_for
from slop_code.execution.enroot_runtime.backend import remove_container
from slop_code.execution.enroot_runtime.exec import EnrootExecRuntime
from slop_code.execution.enroot_runtime.models import EnrootEnvironmentSpec


class MiniSWEEnrootEnvironment:
    """``execute(command) -> {output, returncode}`` for MiniSWE."""

    def __init__(self, workspace: Path, spec: EnrootEnvironmentSpec) -> None:
        self.workspace = workspace
        self.spec = spec

    def get_template_vars(self) -> dict[str, str]:
        """Fields the MiniSWE prompt renderer merges in from the environment."""
        return {
            "cwd": self.spec.enroot.workdir,
            "image": self.spec.enroot.image,
        }

    def execute(self, command: str, timeout: float | None = None) -> dict:
        runtime = EnrootExecRuntime.spawn(
            self.spec,
            self.workspace,
            command,
            disable_setup=True,
            attach_slirp=self.spec.enroot.attach_slirp,
        )
        try:
            result = runtime.execute({}, None, timeout)
        except subprocess.TimeoutExpired:
            runtime.cleanup()
            raise
        finally:
            runtime.cleanup()
        output = result.stdout
        if result.stderr:
            output = f"{output}{result.stderr}"
        if result.timed_out:
            raise subprocess.TimeoutExpired(command, timeout or 0, output=output)
        return {"output": output, "returncode": result.exit_code}

    def cleanup(self) -> None:
        """Drop the unpacked rootfs left behind for exec reuse."""
        remove_container(self.spec, container_name_for(self.workspace))
