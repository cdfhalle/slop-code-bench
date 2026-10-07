#!/bin/bash
# Single source of truth for site-specific settings. Every value is
# env-overridable, so nothing here needs editing to run a one-off differently:
#
#   SCB_MEM=32G SCB_TIME=12:00:00 python -m scb_slurm.submit_batch ...
#
# Sourced by every slurm/*.sbatch and by the login-side submitter. Keep it
# side-effect free (no mkdir, no network, no enroot).

# Repo checkout. Derived from this file's own location so a moved or renamed
# checkout keeps working; override only if you must.
SCB_REPO="${SCB_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Slurm submission. Generation and evaluation are CPU-only: the model is
# reached over HTTP, so no GPU is ever requested.
SCB_ACCOUNT="${SCB_ACCOUNT:-sci-maalej-swe-bench}"
# cpu-batch was renamed. sbatch rejects the old name.
SCB_PARTITION="${SCB_PARTITION:-pot-hpi-cpu-batch}"
# '&' is literal inside this assignment. A shell that word-splits an unquoted
# sbatch line would background at the '&'; submit_batch passes one argv.
SCB_CONSTRAINT="${SCB_CONSTRAINT:-ARCH:X86&SCRATCH:NVME}"

# Read-only shared store of baked images on project storage (backed up, group
# readable). Scratch (below) holds the imported base sqsh and enroot's layer
# cache, which are not backed up and may be purged.
#
# SCB_IMAGES_DIR must NEVER equal SCB_IMAGE_STORE. The store is shared group
# data; scratch is disposable. bake_image.sbatch refuses to export if they
# are the same path.
SCB_IMAGE_STORE="${SCB_IMAGE_STORE:-/sc/projects/sci-maalej/swe-bench/containers/slop-code}"
SCB_IMAGES_DIR="${SCB_IMAGES_DIR:-/sc/scratch/$USER/slop-code/images}"

# Per-run artifacts (problems.txt, run.yaml, harness output). These files are
# not executed, so the repo filesystem (often GPFS) is fine.
SCB_RUNS_DIR="${SCB_RUNS_DIR:-$SCB_REPO/runs}"

# Problem catalog from `slop-code sync`. The harness reads SCBENCH_HOME.
# Kept on the group project dir (the symlink next to this checkout), not in
# ~/.cache, so the download is backed up and shared by later jobs.
SCBENCH_HOME="${SCBENCH_HOME:-$(dirname "$SCB_REPO")/group_project_dir/slop-code/scbench}"

# Python for the harness itself (not the container's interpreter).
SCB_PYTHON="${SCB_PYTHON:-$SCB_REPO/.venv/bin/python}"

# Endpoint descriptors written by the model-serving repo, one file per model.
# The directory is the stable path: each user serves their own model and reads
# their own endpoint/ dir. Look beside this checkout first, then the
# ~/projects/ layout -- that second candidate keeps discovery working from a
# worktree, where "beside this checkout" lands inside .claude/worktrees/ and
# does not exist. run_array does not read these files unless you pass
# --api-base; this only records where they live so a hostname is never baked in.
if [ -z "${SCB_ENDPOINT_DIR:-}" ]; then
    for _d in "$SCB_REPO/../model-hosting/endpoint" "$HOME/projects/model-hosting/endpoint"; do
        [ -d "$_d" ] && { SCB_ENDPOINT_DIR="$_d"; break; }
    done
    unset _d
fi
SCB_ENDPOINT_DIR="${SCB_ENDPOINT_DIR:-$SCB_REPO/../model-hosting/endpoint}"

# Static slirp4netns used when a container must leave the host netns.
# The cluster image does not ship it; see slurm/bin/README.md. Not downloaded here.
SCB_SLIRP="${SCB_SLIRP:-$SCB_REPO/slurm/bin/slirp4netns}"

# Per-task requests, not a packing target. cpu-batch nodes are plentiful, so
# each array task asks for a whole node slice of this size instead of bin-packing
# several problems onto one allocation. submit_batch passes these as sbatch
# flags (they override the #SBATCH defaults). bake_image.sbatch does not use
# them: the image build has its own mem/time, filled in after the first real bake.
SCB_MEM="${SCB_MEM:-16G}"
SCB_CPUS="${SCB_CPUS:-4}"
SCB_TIME="${SCB_TIME:-08:00:00}"

export SCB_REPO SCB_ACCOUNT SCB_PARTITION SCB_CONSTRAINT \
    SCB_IMAGE_STORE SCB_IMAGES_DIR SCB_RUNS_DIR SCB_PYTHON \
    SCB_ENDPOINT_DIR SCB_SLIRP SCB_MEM SCB_CPUS SCB_TIME \
    SCBENCH_HOME
