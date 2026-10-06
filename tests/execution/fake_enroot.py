"""Stand-in for the enroot binary used by unit tests.

Records every invocation. ``create`` writes a rootfs whose ``/etc/rc`` has
no entrypoint. ``start`` execs the container command after rewriting mount
destinations onto the host paths, so the supervisor actually runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _log(argv: list[str]) -> None:
    path = os.environ.get("FAKE_ENROOT_LOG")
    if not path:
        return
    mount_home = os.environ.get("ENROOT_MOUNT_HOME", "")
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(mount_home + "\t" + "\0".join(argv) + "\n")


def _create(argv: list[str]) -> int:
    name = argv[argv.index("--name") + 1]
    root = Path(os.environ["ENROOT_DATA_PATH"]) / name
    etc = root / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "rc").write_text('exec "$@"\n', encoding="utf-8")
    (etc / "environment").write_text("FAKE_IMAGE_ENV=1\n", encoding="utf-8")
    return 0


def _mounts(argv: list[str]) -> dict[str, str]:
    """Map container path -> host path from ``--mount host:container:flags``."""
    found: dict[str, str] = {}
    index = 0
    while index < len(argv) - 1:
        if argv[index] == "--mount":
            parts = argv[index + 1].split(":")
            if len(parts) >= 2:
                found[parts[1]] = parts[0]
            index += 2
            continue
        index += 1
    return found


def _rewrite(token: str, mounts: dict[str, str]) -> str:
    for container, host in mounts.items():
        if token == container or token.startswith(container.rstrip("/") + "/"):
            suffix = token[len(container) :].lstrip("/")
            return str(Path(host) / suffix) if suffix else host
        token = token.replace(container, host)
    return token


def _start(argv: list[str]) -> None:
    mounts = _mounts(argv)
    index = 1
    command: list[str] = []
    while index < len(argv):
        arg = argv[index]
        if arg in {"--mount", "--env"}:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        command = [_rewrite(part, mounts) for part in argv[index + 1 :]]
        break
    if not command:
        sys.stderr.write("fake enroot start: no command\n")
        sys.exit(2)
    os.execvp(command[0], command)  # noqa: S606 - test double replaces itself.


def main(argv: list[str]) -> None:
    _log(argv)
    if not argv or argv[0] not in {"create", "start", "remove"}:
        sys.stderr.write(f"fake enroot: unknown command {argv}\n")
        sys.exit(2)
    if argv[0] == "create":
        sys.exit(_create(argv))
    if argv[0] == "remove":
        if "--name" in argv or len(argv) >= 2:
            name = argv[-1]
            if name not in {"-f", "remove"}:
                root = Path(os.environ.get("ENROOT_DATA_PATH", "")) / name
                if root.is_dir():
                    for child in sorted(root.rglob("*"), reverse=True):
                        if child.is_file():
                            child.unlink()
                        elif child.is_dir():
                            child.rmdir()
                    root.rmdir()
        sys.exit(0)
    _start(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
