#!/bin/bash
# One array task per problem. Scores a finished run directory; no model call.
# Same problem list as run_array ($SCB_RUNS_DIR/$RUN/problems.txt).
#
# `slop-code eval` takes the run directory as a positional argument and
# --env-config for the environment (there is no --environment on eval).
# Its own default pass policy is all-cases; the run default is any. This
# task passes the pass_policy from the generated run.yaml so the two agree.
#
# Directives are defaults. submit_batch overrides account, partition,
# constraint, mem, cpus, and time from config.sh.
#SBATCH --job-name=scb-eval
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=pot-hpi-cpu-batch
#SBATCH --constraint=ARCH:X86&SCRATCH:NVME
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=08:00:00
# stderr joins this file; Slurm opens it before the script runs (logs/ must exist).
#SBATCH --output=logs/%x-%A_%a.out

set -euo pipefail

cd "${SCB_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
# shellcheck source=../config.sh
source ./config.sh
# shellcheck source=enroot_env.sh
source ./slurm/enroot_env.sh

# Same expansion the run used. Eval starts containers too.
export SCB_IMAGE_STORE SCB_SLIRP

if [ ! -f "$ENROOT_SYSCONF_PATH/mounts.d/20-config.fstab" ]; then
    echo "enroot sysconf mirror missing at $ENROOT_SYSCONF_PATH" >&2
    echo "run: bash slurm/setup_enroot_sysconf.sh" >&2
    exit 1
fi

RUN=${RUN:?set RUN}
# Not ENVIRONMENT: the site sets that to BATCH on every batch job.
SCB_ENVIRONMENT=${SCB_ENVIRONMENT:-configs/environments/enroot-python3.12-uv.yaml}
: "${SLURM_ARRAY_TASK_ID:?submit as an array (sbatch --array=...)}"

PROBLEMS_FILE="$SCB_RUNS_DIR/$RUN/problems.txt"
RUN_YAML="$SCB_RUNS_DIR/$RUN/run.yaml"
RUN_DIR="$SCB_RUNS_DIR/$RUN"
if [ ! -f "$PROBLEMS_FILE" ]; then
    echo "missing $PROBLEMS_FILE" >&2
    exit 1
fi
if [ ! -d "$RUN_DIR" ]; then
    echo "missing run directory $RUN_DIR" >&2
    exit 1
fi

PROBLEM=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$PROBLEMS_FILE" | tr -d '\r')
PROBLEM="${PROBLEM#"${PROBLEM%%[![:space:]]*}"}"
PROBLEM="${PROBLEM%"${PROBLEM##*[![:space:]]}"}"
if [ -z "$PROBLEM" ]; then
    echo "no problem on line $((SLURM_ARRAY_TASK_ID + 1)) of $PROBLEMS_FILE" >&2
    exit 1
fi

# Generated yaml quotes the value. Strip quotes; fall back to the run default.
policy=any
if [ -f "$RUN_YAML" ]; then
    policy=$(sed -n 's/^pass_policy:[[:space:]]*//p' "$RUN_YAML" | head -1 | tr -d "\"'[:space:]")
    policy=${policy:-any}
fi

echo "===== eval task $SLURM_ARRAY_TASK_ID  node=$(hostname) ====="
echo "run=$RUN problem=$PROBLEM env=$SCB_ENVIRONMENT pass_policy=$policy"
echo "image_store=$SCB_IMAGE_STORE"

# --overwrite: the run array already graded each checkpoint. Without this,
# eval exits 1 on a problem that already has evaluation.json.
"$SCB_PYTHON" -m slop_code.entrypoints.cli eval \
    "$RUN_DIR" \
    --problem "$PROBLEM" \
    --env-config "$SCB_ENVIRONMENT" \
    --pass-policy "$policy" \
    --overwrite \
    --no-live-progress \
    --num-workers 1
