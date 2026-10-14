#!/bin/bash
# Download the scb-problems release into SCBENCH_HOME.
# Downloads stay off the login node.
#SBATCH --job-name=scb-sync
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=pot-hpi-cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --output=logs/%x-%j.out

set -euo pipefail

cd "${SCB_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
# shellcheck source=../config.sh
source ./config.sh

mkdir -p "$SCBENCH_HOME" logs
echo "SCBENCH_HOME=$SCBENCH_HOME"
"$SCB_PYTHON" -m slop_code.entrypoints.cli sync
echo "catalog ready under $SCBENCH_HOME"