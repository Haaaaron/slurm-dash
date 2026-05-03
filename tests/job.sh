#!/usr/bin/env bash
#SBATCH --job-name=slurm-dash-test
#SBATCH --time=00:01:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=256M
#SBATCH --output=/dev/null

sleep 2
