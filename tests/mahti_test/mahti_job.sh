#!/bin/bash
#SBATCH --job-name=slurm-dash-test
#SBATCH --account=project_<YOUR_PROJECT_ID> # TODO: Replace with your CSC Mahti project ID (e.g., project_2001234)
#SBATCH --partition=test
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --time=00:05:00
#SBATCH --output=slurm-%j.out

# Project number drives every scratch path below. Override at submit time
# with `PROJECTNUM=2001234 sbatch mahti_job.sh` or set it in your shell rc.
export PROJECTNUM=${PROJECTNUM:-<YOUR_PROJECT_NUMBER>}
export SCRATCH_DIR=/scratch/project_${PROJECTNUM}
export PROJAPPL_DIR=/projappl/project_${PROJECTNUM}
export OUTPUT_DIR=${SCRATCH_DIR}/output-${SLURM_JOB_ID}
export RUN_TAG="${USER}-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$OUTPUT_DIR"

echo "Job started on $(hostname) at $(date)"
echo "PROJECTNUM           : $PROJECTNUM"
echo "SCRATCH_DIR          : $SCRATCH_DIR"
echo "PROJAPPL_DIR         : $PROJAPPL_DIR"
echo "OUTPUT_DIR           : $OUTPUT_DIR"
echo "RUN_TAG              : $RUN_TAG"
echo "CUDA_VISIBLE_DEVICES : $CUDA_VISIBLE_DEVICES"

python3 dummy_workload.py 2>&1 | tee "${OUTPUT_DIR}/run-${SLURM_JOB_ID}.log"

echo "Job completed at $(date)" >> "${OUTPUT_DIR}/run-${SLURM_JOB_ID}.log"
