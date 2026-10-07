#!/bin/bash
# Real-image checks for the enroot runtime. Not a login-node command.
#SBATCH --job-name=scb-smoke
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=pot-hpi-cpu-batch
#SBATCH --constraint=ARCH:X86&SCRATCH:NVME
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=02:00:00
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SCB_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
# shellcheck source=../config.sh
source ./config.sh
# shellcheck source=enroot_env.sh
source ./slurm/enroot_env.sh
bash slurm/setup_enroot_sysconf.sh
"$SCB_PYTHON" slurm/smoke_runtime.py
