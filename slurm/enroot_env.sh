#!/bin/bash
# Sourced by every batch script before any enroot process. Not side-effect
# free: it creates the enroot directories it exports.
#
# ENROOT_MOUNT_HOME must be the letter n, not empty. enroot's config loader
# treats an empty value as unset (${var:-default}) and then applies the site
# drop-in, which sets ENROOT_MOUNT_HOME=y. A value of n/no/false is stripped
# by enroot before hooks run, and the home hook only mounts $HOME when the
# variable is still set. Do not mount $HOME: the host home is on shared
# storage, and a writable bind would leak agent state across problems.
#
# The container rootfs and Python's tempfile directory (the session workspace,
# where a .venv's binaries must be executable) stay on node-local disk.
# SLURM_SCRATCH is created by the SCRATCH:NVME task prolog; without that
# constraint the fallback is /tmp, which is also node-local. Results under
# the repo runs/ tree may sit on GPFS; that is fine because those files are
# not executed.

if [ -z "${SCB_REPO:-}" ]; then
    echo "source config.sh before slurm/enroot_env.sh" >&2
    return 1 2>/dev/null || exit 1
fi

# Required. Empty falls back to the site default, which mounts $HOME.
export ENROOT_MOUNT_HOME=n
export ENROOT_SYSCONF_PATH="$SCB_REPO/enroot_sysconf"

if [ -n "${SLURM_SCRATCH:-}" ]; then
    export ENROOT_DATA_PATH="$SLURM_SCRATCH/enroot-data"
    export ENROOT_RUNTIME_PATH="$SLURM_SCRATCH/enroot-run"
    export TMPDIR="$SLURM_SCRATCH/tmp"
else
    export ENROOT_DATA_PATH="/tmp/enroot-$USER/data"
    export ENROOT_RUNTIME_PATH="/tmp/enroot-$USER/run"
    export TMPDIR="/tmp/slop-code/${SLURM_JOB_ID:-local}"
fi

# Layer cache, not the rootfs. Shared scratch is the right size for it; the
# rootfs itself is ENROOT_DATA_PATH above, which is node-local.
export ENROOT_CACHE_PATH="/sc/scratch/$USER/slop-code/enroot/cache"

mkdir -p "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH" "$ENROOT_CACHE_PATH" "$TMPDIR"
