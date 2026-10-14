#!/bin/bash
# Cancel model-serving jobs after the rollout arrays have finished.
# Eval does not call the model, so this does not wait for the re-grade.
#SBATCH --job-name=scb-stop-serve
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:10:00
#SBATCH --output=logs/%x-%j.out

set -u
if [ -z "${SERVE_JOBS:-}" ]; then
    echo "SERVE_JOBS is empty" >&2
    exit 1
fi
status=0
for id in ${SERVE_JOBS//:/ }; do
    echo "scancel $id"
    if ! scancel "$id"; then
        echo "scancel $id returned non-zero (already finished is fine)" >&2
        status=0
    fi
done
exit "$status"
