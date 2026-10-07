"""Cluster smoke for the enroot runtime. Run from slurm/smoke_runtime.sbatch.

Checks a real baked image: a command round-trip, that the host home is not
mounted, HTTPS through slirp4netns, and two containers binding one port.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path

from slop_code.execution.enroot_runtime.exec import EnrootExecRuntime
from slop_code.execution.enroot_runtime.models import EnrootConfig
from slop_code.execution.enroot_runtime.models import EnrootEnvironmentSpec
from slop_code.execution.enroot_runtime.streaming import EnrootStreamingRuntime

PORT = 8765
BIND = f"""python -c 'import socket,time
s=socket.socket()
s.bind(("127.0.0.1",{PORT}))
s.listen(1)
print("bound", flush=True)
time.sleep(30)'"""


def spec() -> EnrootEnvironmentSpec:
    image = str(Path(os.environ["SCB_IMAGE_STORE"]) / "slop-code-python3.12.sqsh")
    return EnrootEnvironmentSpec(
        name="python3.12",
        enroot=EnrootConfig(
            image=image,
            slirp_binary=os.environ["SCB_SLIRP"],
            attach_slirp=True,
            workdir="/workspace",
        ),
    )


def one_shot(workspace: Path, command: str, timeout: float) -> tuple[int, str, str]:
    runtime = EnrootExecRuntime.spawn(
        spec(),
        workspace,
        command,
        disable_setup=True,
        attach_slirp=True,
    )
    try:
        result = runtime.execute({}, None, timeout)
    finally:
        runtime.cleanup()
    return result.exit_code, result.stdout, result.stderr


def bind_until_ready(runtime: EnrootStreamingRuntime, found: list[str]) -> None:
    try:
        for event in runtime.stream(BIND, {}, 40):
            if event.kind == "stdout" and event.text and "bound" in event.text:
                found.append("ok")
                return
            if event.kind == "finished" and event.result is not None:
                if event.result.exit_code != 0:
                    found.append(event.result.stderr[-500:])
                return
    except Exception as exc:  # noqa: BLE001 - report the smoke failure
        if not found or found[-1] != "ok":
            found.append(str(exc))


def main() -> None:
    tmp = Path(os.environ["TMPDIR"])
    work = tmp / "smoke-one"
    work.mkdir(parents=True)
    code, out, err = one_shot(work, "echo smoke-hello", 120)
    if code != 0 or "smoke-hello" not in out:
        raise SystemExit(f"echo failed: {code} {out!r} {err!r}")
    print("echo ok")

    token = f".scb-home-probe-{uuid.uuid4().hex}"
    home = Path(os.environ["HOME"])
    probe = home / token
    code, out, err = one_shot(
        work,
        f"touch {probe} && findmnt -T {home} || true",
        120,
    )
    leaked = probe.exists()
    if leaked:
        probe.unlink()
    if leaked or code != 0:
        raise SystemExit(f"home mount check failed leaked={leaked} {code} {out!r} {err!r}")
    print("home not mounted")

    code, out, err = one_shot(
        work,
        "python - <<'PY'\n"
        "from pathlib import Path\n"
        "print('RESOLV')\n"
        "print(Path('/etc/resolv.conf').read_text(errors='replace'))\n"
        "print('NET', [p.name for p in Path('/sys/class/net').iterdir()])\n"
        "import socket\n"
        "print('DNS', socket.getaddrinfo('pypi.org', 443)[:1])\n"
        "import urllib.request\n"
        "print('HTTP', urllib.request.urlopen('https://pypi.org', timeout=60).status)\n"
        "PY",
        120,
    )
    print(out)
    if err:
        print(err)
    if code != 0 or "HTTP 200" not in out:
        raise SystemExit(f"https failed: {code}")
    print("https ok")

    left = tmp / "smoke-left"
    right = tmp / "smoke-right"
    left.mkdir()
    right.mkdir()
    first = EnrootStreamingRuntime.spawn(
        spec(), left, disable_setup=True, attach_slirp=True
    )
    second = EnrootStreamingRuntime.spawn(
        spec(), right, disable_setup=True, attach_slirp=True
    )
    found: list[str] = []
    threads = [
        threading.Thread(target=bind_until_ready, args=(first, found)),
        threading.Thread(target=bind_until_ready, args=(second, found)),
    ]
    try:
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and found.count("ok") < 2:
            time.sleep(0.2)
    finally:
        first.cleanup()
        second.cleanup()
        for thread in threads:
            thread.join(timeout=10)
    if found.count("ok") != 2:
        raise SystemExit(f"port check failed: {found!r}")
    print("two listeners on", PORT)
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
