"""Shared enroot process helpers.

The host home directory is never a mount. The site default is
``ENROOT_MOUNT_HOME=y``, and an empty value falls back to that default,
so every ``enroot`` subprocess gets ``ENROOT_MOUNT_HOME=n``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path

from slop_code.execution.assets import ResolvedStaticAsset
from slop_code.execution.enroot_runtime.models import EnrootEnvironmentSpec
from slop_code.execution.runtime import SolutionRuntimeError

_EXEC_DISPATCH_RE = re.compile(r'^\s*exec\s+(.*?)"\$@"', re.MULTILINE)
_CREATE_RETRIES = 3


def enroot_subprocess_env() -> dict[str, str]:
    """Environment for the ``enroot`` binary itself, not the container."""
    return os.environ | {"ENROOT_MOUNT_HOME": "n"}


def reject_home_mount(host_path: Path) -> None:
    """Refuse a mount whose source is the real home directory."""
    home = Path.home().resolve()
    try:
        resolved = host_path.resolve()
    except OSError:
        resolved = host_path
    if resolved == home or home in resolved.parents:
        raise SolutionRuntimeError(
            f"refusing to mount host home path {resolved}"
        )


def container_name_for(working_dir: Path) -> str:
    """Stable name so several execs against one workspace share an unpack."""
    digest = hashlib.sha256(str(working_dir.resolve()).encode()).hexdigest()
    return f"scb-{digest[:12]}"


def data_root() -> Path:
    configured = os.environ.get("ENROOT_DATA_PATH")
    if configured:
        return Path(configured)
    return Path.home() / ".local" / "share" / "enroot"


def rootfs_for(name: str) -> Path:
    return data_root() / name


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv is built in this module.
        argv,
        check=False,
        capture_output=True,
        text=True,
        env=enroot_subprocess_env(),
    )


def ensure_created(spec: EnrootEnvironmentSpec, name: str, image: str) -> None:
    """Unpack ``image`` as ``name`` if that rootfs is not already there."""
    root = rootfs_for(name)
    if (root / "etc" / "rc").is_file():
        return
    binary = spec.enroot.binary
    last_error = ""
    for _attempt in range(_CREATE_RETRIES):
        _run([binary, "remove", "-f", name])
        result = _run([binary, "create", "--name", name, image])
        if result.returncode == 0 and (root / "etc" / "rc").is_file():
            return
        last_error = (result.stderr or result.stdout or "").strip()
    raise SolutionRuntimeError(
        f"enroot create failed for {image}: {last_error}"
    )


def needs_explicit_shell(name: str) -> bool:
    """True when /etc/rc execs our argv directly and we must supply bash."""
    rc_path = rootfs_for(name) / "etc" / "rc"
    try:
        rc = rc_path.read_text(errors="replace")
    except OSError:
        return True
    match = _EXEC_DISPATCH_RE.search(rc)
    if match is None:
        return True
    return not match.group(1).strip()


def remove_container(spec: EnrootEnvironmentSpec, name: str) -> None:
    _run([spec.enroot.binary, "remove", "-f", name])


def mount_spec(
    host_path: Path,
    container_path: str,
    *,
    read_only: bool,
) -> str:
    reject_home_mount(host_path)
    # enroot splits on ':' into fstab fields: spec, target, type, options.
    # A bare "rw" is read as the filesystem type, so the bind never happens.
    mode = "ro" if read_only else "rw"
    return f"{host_path}:{container_path}:none:x-create=dir,bind,{mode}"


def build_mounts(
    spec: EnrootEnvironmentSpec,
    working_dir: Path,
    static_assets: dict[str, ResolvedStaticAsset],
    runtime_mounts: dict[str, dict[str, str] | str],
) -> list[str]:
    """Host:container mounts. Later entries win for the same container path."""
    ordered: dict[str, str] = {}
    workdir = spec.enroot.workdir

    if spec.enroot.mount_workspace:
        ordered[workdir] = mount_spec(working_dir, workdir, read_only=False)

    for host_path, container_path in spec.enroot.extra_mounts.items():
        host = Path(host_path)
        if not host.is_absolute():
            host = (working_dir / host).resolve()
        if isinstance(container_path, str):
            prefix = workdir.rstrip("/") + "/"
            if container_path.startswith(prefix) or container_path == workdir:
                raise SolutionRuntimeError(
                    f"mount {container_path} is inside {workdir}"
                )
            ordered[container_path] = mount_spec(
                host, container_path, read_only=True
            )
        else:
            bind = str(container_path.get("bind", ""))
            mode = str(container_path.get("mode", "ro"))
            ordered[bind] = mount_spec(host, bind, read_only=mode == "ro")

    for host_path, container_mapping in runtime_mounts.items():
        host = Path(host_path)
        if isinstance(container_mapping, str):
            ordered[container_mapping] = mount_spec(
                host, container_mapping, read_only=True
            )
        else:
            bind = str(container_mapping.get("bind", ""))
            mode = str(container_mapping.get("mode", "ro"))
            ordered[bind] = mount_spec(host, bind, read_only=mode == "ro")

    for asset in static_assets.values():
        absolute = getattr(asset, "absolute_path", None)
        save_path = getattr(asset, "save_path", None)
        if absolute is None or save_path is None:
            continue
        target = str(Path("/static") / str(save_path))
        ordered[target] = mount_spec(Path(str(absolute)), target, read_only=True)

    return list(ordered.values())


def install_supervisor(spec: EnrootEnvironmentSpec, working_dir: Path) -> None:
    """Copy the stdlib supervisor into the bind-mounted workspace."""
    host_dir = working_dir / ".slop-enroot"
    host_dir.mkdir(parents=True, exist_ok=True)
    script_src = Path(__file__).with_name("container_supervisor.py")
    script_dst = host_dir / "container_supervisor.py"
    script_dst.write_text(script_src.read_text(encoding="utf-8"), encoding="utf-8")


def host_socket(working_dir: Path) -> Path:
    return working_dir / ".slop-enroot" / "sock"


def container_socket(spec: EnrootEnvironmentSpec) -> str:
    return str(Path(spec.enroot.workdir) / ".slop-enroot" / "sock")


def start_argv(
    spec: EnrootEnvironmentSpec,
    name: str,
    mounts: list[str],
    container_env: dict[str, str],
    *,
    once: bool,
) -> list[str]:
    """``enroot start`` argv that launches the in-container supervisor."""
    container_script = str(
        Path(spec.enroot.workdir) / ".slop-enroot" / "container_supervisor.py"
    )
    inner = ["python", container_script]
    if once:
        inner.append("--once")
    inner.append(container_socket(spec))
    argv = [spec.enroot.binary, "start", "--rw", "--net", "--pid"]
    for mount in mounts:
        argv.extend(["--mount", mount])
    for key, value in container_env.items():
        argv.extend(["--env", f"{key}={value}"])
    argv.append(name)
    if needs_explicit_shell(name):
        quoted = " ".join(shlex.quote(part) for part in inner)
        argv.extend(["bash", "-c", quoted])
    else:
        argv.extend(inner)
    return argv


def kill_process_group(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait(timeout=5)
