"""Enroot runtime against a fake enroot binary. No cluster, no daemon."""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from slop_code.execution.enroot_runtime.backend import mount_spec
from slop_code.execution.enroot_runtime.backend import reject_home_mount
from slop_code.execution.enroot_runtime.session import container_resolv_text
from slop_code.execution.enroot_runtime.miniswe import MiniSWEEnrootEnvironment
from slop_code.execution.enroot_runtime.models import EnrootConfig
from slop_code.execution.enroot_runtime.models import EnrootEnvironmentSpec
from slop_code.execution.enroot_runtime.streaming import EnrootStreamingRuntime
from slop_code.execution.runtime import SolutionRuntimeError

FAKE = Path(__file__).with_name("fake_enroot.py")


def _wrapper(tmp: Path) -> Path:
    path = tmp / "enroot"
    path.write_text(
        "#!/bin/bash\n"
        f'exec "{os.environ.get("PYTHON", "") or __import__("sys").executable}" '
        f'"{FAKE}" "$@"\n',
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _spec(tmp: Path, binary: Path) -> EnrootEnvironmentSpec:
    return EnrootEnvironmentSpec(
        name="python3.12",
        enroot=EnrootConfig(
            image="base.sqsh",
            binary=str(binary),
            workdir=str(tmp / "work"),
            attach_slirp=False,
            slirp_binary="slirp4netns",
        ),
    )


def _calls(log: Path) -> list[tuple[str, list[str]]]:
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        mount_home, _, rest = line.partition("\t")
        argv = rest.split("\0") if rest else []
        rows.append((mount_home, argv))
    return rows


def test_container_resolv_keeps_the_host_search_list() -> None:
    text = container_resolv_text(
        "search hpc.sci.hpi.de example.test\n"
        "nameserver 10.130.0.1\n"
        "options rotate ndots:2 edns0\n"
    )

    assert text.splitlines()[0] == "nameserver 10.0.2.3"
    assert "search hpc.sci.hpi.de example.test" in text
    assert "options rotate ndots:2 edns0" in text
    assert "10.130.0.1" not in text


def test_container_resolv_sets_ndots_when_the_host_has_none() -> None:
    text = container_resolv_text("nameserver 10.130.0.1\n")

    assert "nameserver 10.0.2.3" in text
    assert "options ndots:2" in text


def test_mount_spec_uses_file_create_for_a_file(tmp_path: Path) -> None:
    host_file = tmp_path / "config.toml"
    host_file.write_text("[core]\n", encoding="utf-8")
    host_dir = tmp_path / "output"
    host_dir.mkdir()

    file_mount = mount_spec(host_file, "/openhands/config.toml", read_only=True)
    dir_mount = mount_spec(host_dir, "/openhands/output", read_only=False)

    assert file_mount.endswith(":none:x-create=file,bind,ro")
    assert dir_mount.endswith(":none:x-create=dir,bind,rw")


def test_rejects_home_mount() -> None:
    with pytest.raises(SolutionRuntimeError, match="home"):
        reject_home_mount(Path.home() / ".cache")


def test_stream_roundtrip_and_mount_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    log = tmp_path / "enroot.log"
    monkeypatch.setenv("ENROOT_DATA_PATH", str(data))
    monkeypatch.setenv("FAKE_ENROOT_LOG", str(log))
    monkeypatch.delenv("ENROOT_MOUNT_HOME", raising=False)
    # The workspace lives under tmp_path, which may itself be inside $HOME.
    # Point Path.home somewhere else so that is not treated as a home mount.
    home = tmp_path / "unmounted-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    binary = _wrapper(tmp_path)
    runtime = EnrootStreamingRuntime.spawn(
        _spec(tmp_path, binary),
        work,
        disable_setup=True,
        attach_slirp=False,
    )
    try:
        events = list(runtime.stream("echo hello-enroot", {}, None))
    finally:
        runtime.cleanup()

    finished = events[-1]
    assert finished.kind == "finished"
    assert finished.result is not None
    assert finished.result.exit_code == 0
    assert "hello-enroot" in finished.result.stdout

    calls = _calls(log)
    assert calls, "fake enroot was not invoked"
    assert {mount_home for mount_home, _argv in calls} == {"n"}
    flat = "\n".join(" ".join(argv) for _home, argv in calls)
    assert "--rw" in flat
    assert "--net" in flat
    assert "--pid" in flat
    assert "remove" in flat
    home = str(Path.home())
    for _mount_home, argv in calls:
        for arg in argv:
            if arg.startswith("--") or ":" not in arg:
                continue
            source = arg.split(":", 1)[0]
            assert source != home
            assert not source.startswith(home + "/")


def test_miniswe_execute_is_one_shot_without_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    log = tmp_path / "enroot.log"
    home = tmp_path / "unmounted-home"
    home.mkdir()
    monkeypatch.setenv("ENROOT_DATA_PATH", str(data))
    monkeypatch.setenv("FAKE_ENROOT_LOG", str(log))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    env = MiniSWEEnrootEnvironment(work, _spec(tmp_path, _wrapper(tmp_path)))
    try:
        result = env.execute("echo miniswe-ok")
    finally:
        env.cleanup()
    assert result["returncode"] == 0
    assert "miniswe-ok" in result["output"]
    calls = _calls(log)
    assert {mount_home for mount_home, _argv in calls} == {"n"}
    starts = [argv for _home, argv in calls if argv and argv[0] == "start"]
    assert starts
    assert "--net" in starts[0]
    assert "--pid" in starts[0]
    assert any(argv and argv[0] == "remove" for _home, argv in calls)


def test_miniswe_execute_kills_a_command_at_the_default_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    home = tmp_path / "unmounted-home"
    home.mkdir()
    monkeypatch.setenv("ENROOT_DATA_PATH", str(data))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    env = MiniSWEEnrootEnvironment(work, _spec(tmp_path, _wrapper(tmp_path)))
    assert env.timeout == 30
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            env.execute("echo before-timeout; sleep 30", timeout=1)
    finally:
        env.cleanup()
    elapsed = time.monotonic() - started
    assert elapsed < 10
    assert caught.value.timeout == 1
    assert isinstance(caught.value.output, bytes)
    assert b"before-timeout" in caught.value.output
