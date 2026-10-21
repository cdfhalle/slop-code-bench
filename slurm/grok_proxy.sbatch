#!/bin/bash
# OpenAI-compatible front for a SuperGrok OAuth token. Login once on the
# submit node (`python -m scb_slurm.grok_proxy login`), then:
#
#   sbatch slurm/grok_proxy.sbatch
#
# The job prints api_base= and writes ~/.config/scb-grok-oauth/endpoint.json.
# One job per user: a second serve would share the same refresh token.
# This is a forwarder, so it does not take the rollout task size from config.sh.
#SBATCH --job-name=scb-grok-proxy
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=pot-hpi-cpu-batch
#SBATCH --constraint=ARCH:X86&SCRATCH:NVME
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=24:00:00
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SCB_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
# shellcheck source=../config.sh
source ./config.sh
mkdir -p logs

# Compute nodes must reach this port. 127.0.0.1 would only serve this node.
exec "$SCB_PYTHON" -m scb_slurm.grok_proxy serve \
    --host 0.0.0.0 \
    --port "${SCB_GROK_PROXY_PORT:-8766}"
