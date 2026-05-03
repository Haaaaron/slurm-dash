#!/bin/bash
#SBATCH --job-name=slurm-dash-test
#SBATCH --account=project_<YOUR_PROJECT_ID> # TODO: Replace with your LUMI project ID (e.g., project_462000000)
#SBATCH --partition=dev-g
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --gpus-per-node=1
#SBATCH --time=00:05:00
#SBATCH --output=slurm-%j.out

# Project number drives every scratch path below. Override at submit time
# with `PROJECTNUM=462000000 sbatch lumi_job.sh` or set it in your shell rc.
export PROJECTNUM=${PROJECTNUM:-<YOUR_PROJECT_NUMBER>}
export SCRATCH_DIR=/scratch/project_${PROJECTNUM}
export FLASH_DIR=/flash/project_${PROJECTNUM}
export OUTPUT_DIR=${SCRATCH_DIR}/output-${SLURM_JOB_ID}
export RUN_TAG="${USER}-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$OUTPUT_DIR"

echo "Job started on $(hostname) at $(date)"
echo "PROJECTNUM           : $PROJECTNUM"
echo "SCRATCH_DIR          : $SCRATCH_DIR"
echo "FLASH_DIR            : $FLASH_DIR"
echo "OUTPUT_DIR           : $OUTPUT_DIR"
echo "RUN_TAG              : $RUN_TAG"
echo "CUDA_VISIBLE_DEVICES : $CUDA_VISIBLE_DEVICES"
echo "ROCR_VISIBLE_DEVICES : $ROCR_VISIBLE_DEVICES"

python3 dummy_workload.py 2>&1 | tee "${OUTPUT_DIR}/run-${SLURM_JOB_ID}.log"

echo "Job completed at $(date)" >> "${OUTPUT_DIR}/run-${SLURM_JOB_ID}.log"
