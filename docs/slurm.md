# Slurm + enroot

CPU-only batches. The model is reached over HTTP. One array task per problem.
Nothing here calls `sbatch` for you until you run the submitter.

## Setup

```bash
uv sync
mkdir -p logs
```

`SCB_PYTHON` is `$SCB_REPO/.venv/bin/python`. Site defaults live in `config.sh`.
Every value is env-overridable. The file is side-effect free; batch scripts and
`scb_slurm.submit_batch` source it. You do not need to source it yourself.

Copy `.env.example` to `~/.config/slop-code/.env` and fill in the keys. The repo
never writes that file. Do not commit secrets.

`pot-hpi-cpu-batch` nodes are plentiful. `SCB_MEM`, `SCB_CPUS`, and `SCB_TIME` are
per-task requests, not a packing target. The submitter passes them as `sbatch`
flags, which override the `#SBATCH` lines (Slurm reads those before the script
runs).

## Bake

```bash
sbatch slurm/bake_image.sbatch
```

This imports `ghcr.io/astral-sh/uv:python3.12-trixie-slim` (public GHCR, not
Docker Hub) into `SCB_IMAGES_DIR` and exports `slop-code-python3.12.sqsh` to
`SCB_IMAGE_STORE`. `ENROOT_MOUNT_HOME=n` for that job too: an empty value falls
back to the site default, which bind-mounts `$HOME`. The bake does not pass
`--net`; a new netns would be loopback only, and apt/curl need the host route.

`HOME`, the cargo/rustup trees, and the uv/pip caches are inside the image at
`/opt/agent_home` and `/opt/cache`. enroot covers `/tmp` with a tmpfs, so those
must not live there.

Optional agent layer (both variables must be exported):

```bash
SCB_BAKE_AGENT=claude_code SCB_AGENT_VERSION=2.0.51 sbatch slurm/bake_image.sbatch
```

That runs the agent's `docker.j2` only when it is a single `npm install`,
`pip install`, or `curl | sh`. Anything else exits 2 and names the file.
`claude_code` is the npm install. The first real bake should record its job id,
wall time, and peak RSS on the `#SBATCH --mem` / `--time` lines in
`slurm/bake_image.sbatch`. Those numbers are placeholders.

A Docker Hub pull is not implemented. It has to use
`docker://registry-1.docker.io#...` plus `DOCKERHUB_USERNAME` / `DOCKERHUB_PAT`
from the XDG env file. The cluster NAT IP's anonymous quota is spent.

## Submit one problem

```bash
uv run python -m scb_slurm.submit_batch \
  --model anthropic/sonnet-4.5 \
  --problems code_search
```

`--model` is `{provider}/{model}` split on the first slash. `--agent` defaults
to `configs/agents/miniswe.yaml`, `--environment` to
`configs/environments/enroot-python3.12-uv.yaml`. There is no `--output-dir`.
The submitter writes `runs/<run>/run.yaml` (`save_dir` is the run directory,
`save_template` is `.`) and `runs/<run>/problems.txt`, builds the enroot sysconf
mirror once, then submits `run_array` and `eval_array` with `afterok`.

`--dry-run` prints the `sbatch` lines and does not submit. `--eval-only`
submits only the eval array. `--api-base URL` sets `OPENAI_API_BASE` in the
run job for a self-hosted server. `--throttle` (default 10) caps concurrent
tasks.

`slop-code run` is invoked with `--config`, `--agent`, `--model`,
`--environment`, `--problem`, and `--no-live-progress`. It grades each
checkpoint before the next one. The second array is `slop-code eval
<run-dir> --problem ... --env-config ... --overwrite`, which re-grades that
saved directory without calling the model. Eval's own default pass policy is
`all-cases`; the generated yaml uses `any` (the run default) and the eval
task passes that through.

One failed run task fails the array's `afterok`, so eval does not start.
Resubmit with `--eval-only` after a successful run if you need to.

## Where results go

`runs/<run>/<problem>/` (or `SCB_RUNS_DIR` if you moved it). The harness writes
there directly. Logs are `logs/scb-<run>-<job>_<task>.out`.

Those result files may sit on GPFS. That is fine: they are not executed. The
container rootfs and Python's tempfile directory (`TMPDIR`, where a `.venv`
must be executable) are node-local: `$SLURM_SCRATCH` on `SCRATCH:NVME` nodes,
otherwise `/tmp`.

## Storage

| path | what |
|---|---|
| `SCB_IMAGES_DIR` | scratch. Imported base sqsh. Not backed up. |
| `SCB_IMAGE_STORE` | project storage. Exported images. Must never equal `SCB_IMAGES_DIR`. |
| `SCB_RUNS_DIR` | run yaml, problem list, harness output. |
| `SCBENCH_HOME` | `slop-code sync` catalog. Under `group_project_dir/slop-code/scbench`. |
| `enroot_sysconf/` | generated mirror. Absolute path baked into the hosts mount; rebuild with `bash slurm/setup_enroot_sysconf.sh` after moving the checkout. |

`ENROOT_CACHE_PATH` is on shared scratch. `ENROOT_DATA_PATH` is node-local.

## What cannot run

No catalog test starts a Docker daemon. The only problem that talks about
`docker` as a program is `recli`: its tests put a fake `docker` script on
`PATH` and expect the solution to call it. The base image ships the Docker
CLI and not a daemon, so a solution that reaches the real `docker` binary
fails. `file_backup` only mentions the word inside a fixture note.

`slirp4netns` is not installed on the cluster. A static 1.3.6 binary belongs
at `slurm/bin/slirp4netns` and is gitignored. See `slurm/bin/README.md`.
