"""Submit the slop-code run array and the eval array that depends on it.

Site defaults come from config.sh (sourced in a subprocess). This does not
call the model. With --dry-run it prints the sbatch commands and does not
submit. Run from a login node.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer

REPO = Path(__file__).resolve().parents[1]
SLURM = REPO / "slurm"

app = typer.Typer(add_completion=False)

_RUN_NAME = re.compile(r"[A-Za-z0-9._-]+")
_SITE_FLAGS = (
    ("SCB_ACCOUNT", "--account"),
    ("SCB_PARTITION", "--partition"),
    ("SCB_CONSTRAINT", "--constraint"),
    ("SCB_MEM", "--mem"),
    ("SCB_CPUS", "--cpus-per-task"),
    ("SCB_TIME", "--time"),
)
_REQUIRED = (
    "SCB_REPO",
    "SCB_ACCOUNT",
    "SCB_PARTITION",
    "SCB_CONSTRAINT",
    "SCB_RUNS_DIR",
    "SCB_MEM",
    "SCB_CPUS",
    "SCB_TIME",
)


def _yaml_double(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _load_site_env(repo: Path) -> dict[str, str]:
    """Source config.sh and return that process environment.

    The shell file is the only copy of the site defaults.
    """
    script = (
        "set -euo pipefail; set -a; source "
        + shlex.quote(str(repo / "config.sh"))
        + "; set +a; env -0"
    )
    try:
        proc = subprocess.run(  # noqa: S603
            ["bash", "-c", script],  # noqa: S607
            cwd=repo,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        sys.stderr.buffer.write(exc.stderr)
        raise typer.Exit(exc.returncode or 1) from exc
    env: dict[str, str] = {}
    for item in proc.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key_b, value_b = item.split(b"=", 1)
        env[key_b.decode("utf-8", "surrogateescape")] = value_b.decode(
            "utf-8", "surrogateescape"
        )
    missing = [key for key in _REQUIRED if not env.get(key)]
    if missing:
        typer.echo(
            "config.sh did not set: " + ", ".join(missing),
            err=True,
        )
        raise typer.Exit(1)
    return env


def _run_name(raw: str) -> str:
    name = raw or datetime.now().strftime("run-%Y%m%d-%H%M")
    if _RUN_NAME.fullmatch(name) is None:
        raise typer.BadParameter(
            "run name must be one path component: letters, digits, '.', '_', '-'"
        )
    return name


def _split_model(spec: str) -> tuple[str, str]:
    """Match slop-code --model: '{provider}/{model}', first slash only."""
    value = spec.strip()
    if "/" not in value:
        raise typer.BadParameter(
            "model must be '{provider}/{model}' "
            f"(first slash splits; got {value!r})"
        )
    provider, name = value.split("/", 1)
    provider = provider.strip()
    name = name.strip()
    if not provider or not name:
        raise typer.BadParameter(
            "model must be '{provider}/{model}' with both sides set"
        )
    return provider, name


def _shard_plan(
    n_items: int, bases: list[str], throttle: int
) -> list[tuple[str, str]]:
    """Split an array across servers.

    ``throttle`` is the total number of rollouts in flight. Each server gets
    an equal share, so 16 rollouts and 2 servers is 8 apiece, matching one
    SGLang ``--max-running-requests 8`` per GPU. Returns
    ``(api_base, array_spec)`` with array indexes into the shared problem
    list. An empty ``bases`` list is one shard and no ``API_BASE``.
    """
    servers = bases or [""]
    if throttle < 1 or throttle % len(servers) != 0:
        raise typer.BadParameter(
            f"--throttle {throttle} must be a positive multiple of "
            f"{len(servers)} server(s)"
        )
    per = throttle // len(servers)
    counts = [n_items // len(servers)] * len(servers)
    for index in range(n_items % len(servers)):
        counts[index] += 1
    plan: list[tuple[str, str]] = []
    start = 0
    for base, count in zip(servers, counts, strict=True):
        if count == 0:
            continue
        end = start + count - 1
        plan.append((base, f"{start}-{end}%{per}"))
        start = end + 1
    return plan


def _clean_problems(names: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if name in seen:
            continue
        if "/" in name or "\n" in name or "\x00" in name:
            raise typer.BadParameter(f"invalid problem name: {name!r}")
        seen.add(name)
        cleaned.append(name)
    return cleaned


def _warn_if_missing(repo: Path, category: str, value: str) -> None:
    """Warn when a config path is not a file yet.

    Bare names are resolved the same way the loader does for the package
    configs directory. A missing enroot yaml is only a warning: another
    change may still be landing it before the job starts.
    """
    path = Path(value).expanduser()
    candidates = (
        path,
        repo / value,
        repo / "configs" / category / f"{value}.yaml",
    )
    if any(candidate.is_file() for candidate in candidates):
        return
    typer.echo(f"warning: {category} config not found yet: {value}", err=True)


def _write_run_yaml(
    path: Path,
    *,
    agent: str,
    environment: str,
    provider: str,
    model_name: str,
    problems: list[str],
    save_dir: Path,
) -> None:
    """Run config. save_dir + save_template '.' is the output directory.

    slop-code run has no --output-dir. CLI --problem replaces `problems`
    for a single array task; the list is here so the file is a valid run
    on its own.
    """
    lines = [
        "# Generated by scb_slurm.submit_batch.",
        "# Array tasks pass --problem, which replaces this list for that task.",
        "# save_template '.' keeps the output in save_dir (no --output-dir).",
        f"agent: {_yaml_double(agent)}",
        f"environment: {_yaml_double(environment)}",
        'prompt: "just-solve"',
        "model:",
        f"  provider: {_yaml_double(provider)}",
        f"  name: {_yaml_double(model_name)}",
        'thinking: "none"',
        'pass_policy: "any"',
        "problems:",
    ]
    lines.extend(f"  - {_yaml_double(name)}" for name in problems)
    lines.append(f"save_dir: {_yaml_double(str(save_dir))}")
    lines.append('save_template: "."')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export_value(key: str, value: str) -> str:
    # sbatch --export is comma-separated. A comma or whitespace splits the list.
    if any(char in value for char in ", \t\n"):
        raise typer.BadParameter(
            f"{key}={value!r} cannot be passed through sbatch --export"
        )
    return f"{key}={value}"


def _sbatch_cmd(
    script: Path,
    env: dict[str, str],
    *,
    array: str,
    job_name: str,
    exports: dict[str, str],
    dependency: str = "",
    use_site: bool = True,
) -> list[str]:
    cmd = ["sbatch", "--parsable"]
    if use_site:
        cmd.extend(f"{flag}={env[var]}" for var, flag in _SITE_FLAGS)
    else:
        cmd.append(f"--account={env['SCB_ACCOUNT']}")
        cmd.append(f"--partition={env['SCB_PARTITION']}")
        cmd.extend(["--cpus-per-task=1", "--mem=1G", "--time=00:10:00"])
    cmd.append(f"--job-name={job_name}")
    if array:
        cmd.append(f"--array={array}")
    if dependency:
        cmd.append(f"--dependency={dependency}")
    export_items = ",".join(
        _export_value(key, value) for key, value in exports.items()
    )
    cmd.append(f"--export=ALL,{export_items}")
    cmd.append(str(script))
    return cmd


def _submit(cmd: list[str], env: dict[str, str]) -> str:
    try:
        proc = subprocess.run(  # noqa: S603
            cmd,
            cwd=REPO,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            typer.echo(exc.stderr, err=True)
        raise typer.Exit(exc.returncode or 1) from exc
    job_id = proc.stdout.strip().split(";", 1)[0]
    if not job_id:
        typer.echo("sbatch returned no job id", err=True)
        raise typer.Exit(1)
    return job_id


@app.command()
def main(
    run: str = typer.Option(
        "",
        "--run",
        help="Run name under SCB_RUNS_DIR. Default: run-YYYYmmdd-HHMM.",
    ),
    problems: list[str] = typer.Option(
        [],
        "--problems",
        help="Problem name. Repeat for more than one.",
    ),
    problems_file: Path | None = typer.Option(
        None,
        "--problems-file",
        exists=True,
        dir_okay=False,
        help="One problem name per line. Combined with --problems.",
    ),
    agent: str = typer.Option(
        "configs/agents/miniswe.yaml",
        "--agent",
        help="Agent config path or bare name.",
    ),
    model: str = typer.Option(
        ...,
        "--model",
        help="'{provider}/{model}'. The first slash splits provider from name.",
    ),
    environment: str = typer.Option(
        "configs/environments/enroot-python3.12-uv.yaml",
        "--environment",
        help="Environment config. ${SCB_IMAGE_STORE} is exported in the job.",
    ),
    throttle: int = typer.Option(
        10,
        "--throttle",
        help=(
            "Total concurrent rollouts. With more than one --api-base, "
            "split evenly across the servers."
        ),
    ),
    api_base: list[str] = typer.Option(
        [],
        "--api-base",
        help=(
            "OpenAI-compatible base URL. Repeat once per server. "
            "Each server receives an equal share of --throttle."
        ),
    ),
    serve_job: list[str] = typer.Option(
        [],
        "--serve-job",
        help=(
            "Slurm id of a model server. Repeat per server. After every "
            "rollout array has finished, a short job cancels these ids. "
            "The re-grade does not use them."
        ),
    ),
    *,
    eval_only: bool = typer.Option(
        False,  # noqa: FBT003
        "--eval-only",
        help="Submit only eval_array (the run directory must already exist).",
    ),
    dry_run: bool = typer.Option(
        False,  # noqa: FBT003
        "--dry-run",
        help="Write the run files and print sbatch. Do not submit or call the model.",
    ),
) -> None:
    """Write the run files and submit run_array, then eval_array afterok."""
    run_name = _run_name(run)
    if throttle < 1:
        raise typer.BadParameter("--throttle must be >= 1")
    names = list(problems)
    if problems_file is not None:
        names = problems_file.read_text(encoding="utf-8").splitlines() + names
    selected = _clean_problems(names)
    if not selected:
        raise typer.BadParameter("pass --problems and/or --problems-file")
    provider, model_name = _split_model(model)

    env = _load_site_env(REPO)
    runs_root = Path(env["SCB_RUNS_DIR"])
    if not runs_root.is_absolute():
        runs_root = REPO / runs_root
    run_dir = runs_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    # Slurm opens #SBATCH --output before the script runs.
    (REPO / "logs").mkdir(parents=True, exist_ok=True)
    (run_dir / "problems.txt").write_text(
        "\n".join(selected) + "\n",
        encoding="utf-8",
    )
    _write_run_yaml(
        run_dir / "run.yaml",
        agent=agent,
        environment=environment,
        provider=provider,
        model_name=model_name,
        problems=selected,
        save_dir=run_dir,
    )
    _warn_if_missing(REPO, "agents", agent)
    _warn_if_missing(REPO, "environments", environment)

    bases = [item.strip() for item in api_base if item.strip()]
    for job_id in serve_job:
        if not job_id.isdigit():
            raise typer.BadParameter(f"serve job id must be numeric, got {job_id!r}")
    shards = _shard_plan(len(selected), bases, throttle)
    setup = ["bash", str(SLURM / "setup_enroot_sysconf.sh")]
    eval_exports = {
        "RUN": run_name,
        "SCB_ENVIRONMENT": environment,
    }
    rel = run_dir
    try:
        rel = run_dir.relative_to(REPO)
    except ValueError:
        rel = run_dir
    shard_desc = ", ".join(spec for _base, spec in shards)
    typer.echo(
        f"run: {rel}   problems: {len(selected)}   "
        f"servers: {len(shards)}   (arrays {shard_desc})"
    )

    def _run_exports(base: str) -> dict[str, str]:
        exports = {
            "RUN": run_name,
            "AGENT": agent,
            "MODEL": model,
            "SCB_ENVIRONMENT": environment,
        }
        if base:
            exports["API_BASE"] = base
        return exports

    def _names(kind: str, index: int) -> str:
        if len(shards) == 1:
            return f"scb-{run_name}" if kind == "run" else f"scb-{run_name}-eval"
        return f"scb-{run_name}-s{index}" if kind == "run" else f"scb-{run_name}-s{index}-eval"

    commands: list[tuple[str, list[str]]] = []
    if not eval_only:
        commands.append(("setup", setup))
        for index, (base, array) in enumerate(shards):
            commands.append(
                (
                    "run",
                    _sbatch_cmd(
                        SLURM / "run_array.sbatch",
                        env,
                        array=array,
                        job_name=_names("run", index),
                        exports=_run_exports(base),
                    ),
                )
            )
            commands.append(
                (
                    "eval",
                    _sbatch_cmd(
                        SLURM / "eval_array.sbatch",
                        env,
                        array=array,
                        job_name=_names("eval", index),
                        exports=eval_exports,
                        dependency="afterok:DRY",
                    ),
                )
            )
        if serve_job:
            commands.append(
                (
                    "stop",
                    _sbatch_cmd(
                        SLURM / "stop_servers.sbatch",
                        env,
                        array="",
                        job_name=f"scb-{run_name}-stop",
                        exports={"SERVE_JOBS": ":".join(serve_job)},
                        dependency="afterany:DRY",
                        use_site=False,
                    ),
                )
            )
    else:
        whole = f"0-{len(selected) - 1}%{throttle}"
        commands.append(
            (
                "eval",
                _sbatch_cmd(
                    SLURM / "eval_array.sbatch",
                    env,
                    array=whole,
                    job_name=f"scb-{run_name}-eval",
                    exports=eval_exports,
                ),
            )
        )

    if dry_run:
        typer.echo("dry-run: not submitting")
        for kind, cmd in commands:
            shown = [
                part.replace("afterok:DRY", "afterok:<run-array-jobid>").replace(
                    "afterany:DRY", "afterany:<run-array-jobids>"
                )
                for part in cmd
            ]
            typer.echo(f"{kind}: {shlex.join(shown)}")
        return

    run_jobs: list[str] = []
    if not eval_only:
        subprocess.run(setup, cwd=REPO, check=True)  # noqa: S603
        for index, (base, array) in enumerate(shards):
            run_job = _submit(
                _sbatch_cmd(
                    SLURM / "run_array.sbatch",
                    env,
                    array=array,
                    job_name=_names("run", index),
                    exports=_run_exports(base),
                ),
                env,
            )
            run_jobs.append(run_job)
            eval_job = _submit(
                _sbatch_cmd(
                    SLURM / "eval_array.sbatch",
                    env,
                    array=array,
                    job_name=_names("eval", index),
                    exports=eval_exports,
                    dependency=f"afterok:{run_job}",
                ),
                env,
            )
            label = base or "default"
            typer.echo(f"run_array[{index}] {label}: {run_job}  ({array})")
            typer.echo(f"eval_array[{index}]: {eval_job}  (afterok:{run_job})")
        if serve_job:
            stop_job = _submit(
                _sbatch_cmd(
                    SLURM / "stop_servers.sbatch",
                    env,
                    array="",
                    job_name=f"scb-{run_name}-stop",
                    exports={"SERVE_JOBS": ":".join(serve_job)},
                    dependency="afterany:" + ":".join(run_jobs),
                    use_site=False,
                ),
                env,
            )
            typer.echo(
                f"stop_servers: {stop_job}  (afterany:{':'.join(run_jobs)}) "
                f"cancels {' '.join(serve_job)}"
            )
    else:
        eval_job = _submit(commands[0][1], env)
        typer.echo(f"eval_array: {eval_job}")
    typer.echo(f"logs: logs/scb-{run_name}*.out   results: {rel}/<problem>/")


if __name__ == "__main__":
    app()
