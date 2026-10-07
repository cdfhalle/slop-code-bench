#!/bin/bash
# One array task per problem. submit_batch is the normal entry point:
#
#   python -m scb_slurm.submit_batch --model anthropic/sonnet-4.5 --problems NAME
#
# Slurm reads #SBATCH before this script, so the directives below are only the
# defaults. submit_batch passes account, partition, constraint, mem, cpus, and
# time from config.sh, which overrides them. Nodes are plentiful: those are
# per-task sizes, not a packing target.
#
# There is no --output-dir. submit_batch writes $SCB_RUNS_DIR/$RUN/run.yaml
# with save_dir pointed at that run directory; this task passes --config.
#SBATCH --job-name=scb-run
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=pot-hpi-cpu-batch
#SBATCH --constraint=ARCH:X86&SCRATCH:NVME
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=08:00:00
# stderr joins this file; Slurm opens it before the script runs (logs/ must exist).
#SBATCH --output=logs/%x-%A_%a.out

set -euo pipefail

# Blank placeholders in the example file must not clobber a key that is already
# set. Values are taken literally (no $ expansion).
load_cred_file() {
    local file=$1 line key val
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line#"${line%%[![:space:]]*}"}
        case "$line" in
            '' | \#*) continue ;;
        esac
        case "$line" in
            export\ *) line=${line#export } ;;
        esac
        key=${line%%=*}
        val=${line#*=}
        [ "$key" = "$line" ] && continue
        key=${key%"${key##*[![:space:]]}"}
        key=${key#"${key%%[![:space:]]*}"}
        if [ "${#val}" -ge 2 ]; then
            case "$val" in
                \"*\") val=${val#\"}; val=${val%\"} ;;
                \'*\') val=${val#\'}; val=${val%\'} ;;
            esac
        fi
        [ -n "$key" ] && [ -n "$val" ] || continue
        export "$key=$val"
    done <"$file"
}

cd "${SCB_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
# shellcheck source=../config.sh
source ./config.sh
# shellcheck source=enroot_env.sh
source ./slurm/enroot_env.sh

# The enroot environment yaml expands ${SCB_IMAGE_STORE}. config.sh exports it;
# keep the export next to the run so a yaml change is not the only reminder.
export SCB_IMAGE_STORE SCB_SLIRP

if [ ! -f "$ENROOT_SYSCONF_PATH/mounts.d/20-config.fstab" ]; then
    echo "enroot sysconf mirror missing at $ENROOT_SYSCONF_PATH" >&2
    echo "run: bash slurm/setup_enroot_sysconf.sh" >&2
    exit 1
fi

# load_dotenv() in the CLI only sees a .env next to the working directory.
# Keys live in the XDG file the operator copies from .env.example; this repo
# never writes that path.
load_cred_file "${XDG_CONFIG_HOME:-$HOME/.config}/slop-code/.env"

RUN=${RUN:?set RUN}
AGENT=${AGENT:-configs/agents/miniswe.yaml}
# Not ENVIRONMENT: the site sets that to BATCH on every batch job.
SCB_ENVIRONMENT=${SCB_ENVIRONMENT:-configs/environments/enroot-python3.12-uv.yaml}
MODEL=${MODEL:?set MODEL as provider/name}
: "${SLURM_ARRAY_TASK_ID:?submit as an array (sbatch --array=...)}"

PROBLEMS_FILE="$SCB_RUNS_DIR/$RUN/problems.txt"
RUN_YAML="$SCB_RUNS_DIR/$RUN/run.yaml"
if [ ! -f "$PROBLEMS_FILE" ]; then
    echo "missing $PROBLEMS_FILE" >&2
    exit 1
fi
if [ ! -f "$RUN_YAML" ]; then
    echo "missing $RUN_YAML" >&2
    exit 1
fi

PROBLEM=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$PROBLEMS_FILE" | tr -d '\r')
PROBLEM="${PROBLEM#"${PROBLEM%%[![:space:]]*}"}"
PROBLEM="${PROBLEM%"${PROBLEM##*[![:space:]]}"}"
if [ -z "$PROBLEM" ]; then
    echo "no problem on line $((SLURM_ARRAY_TASK_ID + 1)) of $PROBLEMS_FILE" >&2
    exit 1
fi

# --api-base from the submitter. Litellm reads OPENAI_API_BASE and refuses to
# send when OPENAI_API_KEY is empty. claude_code does not read ANTHROPIC_BASE_URL
# unless its own config sets base_url; the variable is still exported for
# clients that do honor it. An explicit API_BASE wins; this script does not
# scan SCB_ENDPOINT_DIR, so a stale descriptor cannot redirect a hosted model.
if [ -n "${API_BASE:-}" ]; then
    export OPENAI_API_BASE="$API_BASE"
    export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-$API_BASE}"
    if [ -z "${OPENAI_API_KEY:-}" ]; then
        export OPENAI_API_KEY=local
    fi
    echo "api_base=$API_BASE"
fi

echo "===== run task $SLURM_ARRAY_TASK_ID  node=$(hostname) ====="
echo "run=$RUN problem=$PROBLEM agent=$AGENT model=$MODEL"
echo "environment=$SCB_ENVIRONMENT config=$RUN_YAML"
echo "image_store=$SCB_IMAGE_STORE tmpdir=$TMPDIR"

# Fallback only. The run.yaml save_dir is the run directory, so slop-code
# writes $SCB_RUNS_DIR/$RUN/<problem> directly. If a task was launched with
# the repo-level outputs/ default instead, bring that tree across.
# Never cp -a: it preserves ownership and can fail across filesystems.
copy_fallback_outputs() {
    local status=$?
    local dest="$SCB_RUNS_DIR/$RUN"
    local src="$SCB_REPO/outputs"
    local dest_real src_real
    if [ -d "$dest/$PROBLEM" ] || [ -f "$dest/config.yaml" ]; then
        exit "$status"
    fi
    if [ ! -d "$src" ]; then
        exit "$status"
    fi
    dest_real=$(realpath -m "$dest")
    src_real=$(realpath -m "$src")
    case "$dest_real" in
        "$src_real" | "$src_real"/*) exit "$status" ;;
    esac
    mkdir -p "$dest"
    if ! cp -r --preserve=timestamps "$src/." "$dest/"; then
        echo "failed to copy $src into $dest" >&2
        if [ "$status" -eq 0 ]; then
            status=1
        fi
    fi
    exit "$status"
}
trap copy_fallback_outputs EXIT

# Inline evaluation stays on. `slop-code run` grades each checkpoint before
# the next one, and early-stop reads that grade. eval_array re-grades the
# saved directory with --overwrite, so it does not exit 1 on those results.
# --no-live-progress: there is no TTY; the progress display corrupts the log.
"$SCB_PYTHON" -m slop_code.entrypoints.cli run \
    --config "$RUN_YAML" \
    --agent "$AGENT" \
    --model "$MODEL" \
    --environment "$SCB_ENVIRONMENT" \
    --problem "$PROBLEM" \
    --no-live-progress
