#!/usr/bin/env bash
# Stress test: submit N jobs as fast as possible and report capture overhead.
#
# Usage:
#   ./stress_submit.sh [N]          # submit N jobs (default: 20)
#   ./stress_submit.sh [N] --seq    # submit sequentially instead of in parallel
#
# Run this from the remote cluster after slurm-dash add so the sbatch wrapper
# is active. After the run, check captures landed in the DB:
#   sqlite3 ~/.slurm_tracker/.slurm_tracker.db \
#       "SELECT job_id, submit_time FROM jobs ORDER BY submit_time DESC LIMIT $N;"

set -euo pipefail

N=${1:-20}
PARALLEL=true
if [[ "${2:-}" == "--seq" ]]; then PARALLEL=false; fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_SCRIPT="$SCRIPT_DIR/job.sh"

if [[ ! -f "$JOB_SCRIPT" ]]; then
    echo "ERROR: $JOB_SCRIPT not found" >&2
    exit 1
fi

echo "Submitting $N jobs ($( $PARALLEL && echo parallel || echo sequential ))..."
echo "Using wrapper: $(which sbatch)"
echo ""

JOB_IDS=()
ERRORS=0
START=$(date +%s%3N)

submit_one() {
    local i=$1
    local out
    out=$(sbatch --job-name="stress-$i" "$JOB_SCRIPT" 2>&1) && \
        echo "$out" || { echo "FAILED[$i]: $out" >&2; return 1; }
}

if $PARALLEL; then
    for i in $(seq 1 "$N"); do
        submit_one "$i" &
    done
    wait
else
    for i in $(seq 1 "$N"); do
        submit_one "$i"
    done
fi

END=$(date +%s%3N)
ELAPSED=$(( END - START ))

echo ""
echo "Done. $N submissions in ${ELAPSED}ms ($((ELAPSED / N))ms avg per job)"
echo ""
echo "Check capture DB:"
echo "  sqlite3 ~/.slurm_tracker/.slurm_tracker.db \\"
echo "    \"SELECT job_id, submit_time FROM jobs ORDER BY submit_time DESC LIMIT $N;\""
