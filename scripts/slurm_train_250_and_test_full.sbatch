#!/usr/bin/env bash
#SBATCH --job-name=openzl_fav4_train_test
#SBATCH --cpus-per-task=16
#SBATCH --time=04:00:00
#SBATCH --output=slurm-%x-%j.out
#SBATCH --error=slurm-%x-%j.err

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

# You can override these at submission time, e.g.:
#   sbatch --export=ALL,TARGET_MIB=200,THREADS=16,MAX_TIME_SECS=1800 scripts/slurm_train_250_and_test_full.sbatch

export THREADS="${THREADS:-16}"
export TARGET_MIB="${TARGET_MIB:-200}"
export MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"
export TRAIN_PRE_THREADS="${TRAIN_PRE_THREADS:-1}"
export FULL_PRE_THREADS="${FULL_PRE_THREADS:-16}"
export VALIDATE_FULL="${VALIDATE_FULL:-1}"

bash scripts/run_train_250_and_test_full.sh
