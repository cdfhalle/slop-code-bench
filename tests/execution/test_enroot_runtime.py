"""Enroot runtime against a fake enroot binary. No cluster, no daemon."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from slop_code.execution.enroot_runtime.backend import reject_home_mount
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
