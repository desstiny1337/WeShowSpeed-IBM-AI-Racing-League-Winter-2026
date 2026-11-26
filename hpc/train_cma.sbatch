#!/bin/bash
# CMA-ES driver parameter search on PERUN (spec 0010).
#
#   sbatch hpc/train_cma.sbatch [extra train_cma.py args]
#   OUTDIR=/mnt/scratch/$USER/cma_runs/run1 sbatch --export=ALL hpc/train_cma.sbatch --popsize 20
#
# If $OUTDIR/checkpoint.pkl exists the run is resumed automatically, so the
# same command (same OUTDIR) continues a run after the time limit.
#
# Environment (all optional):
#   CODE_DIR   repo copy on the cluster      [/mnt/scratch/$USER/gym_torcs]
#   OUTDIR     output directory              [/mnt/scratch/$USER/cma_runs/run_$SLURM_JOB_ID]
#   PROJECT    project dir                   [/mnt/project/$SLURM_JOB_ACCOUNT]
#   SIF        apptainer image               [$PROJECT/torcs/torcs-env.sif]
#   PYLIB      numpy + cma for Python 3.10   [$PROJECT/torcs/pylib]
#
# Account and QoS are not in the header: export SBATCH_ACCOUNT and SBATCH_QOS
# (both = project id) once, e.g. in ~/.bashrc on the cluster (hpc/README.md).
#   MAX_HOURS  stop cleanly after this        [46.5] (keep below -t)
#   WORKERS    parallel evaluations, <= 10    [10]
#
#SBATCH --job-name=torcs-cma
#SBATCH --partition=cpu_short
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=12
#SBATCH --mem=16G
#SBATCH --time=47:00:00
#SBATCH --output=/mnt/scratch/%u/cma_logs/%x-%j.out

set -euo pipefail

PROJECT=${PROJECT:-/mnt/project/${SLURM_JOB_ACCOUNT:?submit with an account (SBATCH_ACCOUNT)}}
SCRATCH=/mnt/scratch/$USER
CODE_DIR=${CODE_DIR:-$SCRATCH/gym_torcs}
OUTDIR=${OUTDIR:-$SCRATCH/cma_runs/run_${SLURM_JOB_ID:-manual}}
SIF=${SIF:-$PROJECT/torcs/torcs-env.sif}
PYLIB=${PYLIB:-$PROJECT/torcs/pylib}
MAX_HOURS=${MAX_HOURS:-46.5}
WORKERS=${WORKERS:-10}

# TORCS writes ~/.torcs on start: HOME must be writable.
export HOME=$SCRATCH/torcs_home
export PYTHONPATH=$PYLIB
mkdir -p "$HOME" "$OUTDIR" "$SCRATCH/cma_logs"

RESUME=()
if [ -f "$OUTDIR/checkpoint.pkl" ]; then
    RESUME=(--resume --port-check-on-resume)
fi

echo "job ${SLURM_JOB_ID:-} on $(hostname), $(date)"
echo "code $CODE_DIR ($(cd "$CODE_DIR" && git rev-parse --short HEAD 2>/dev/null || echo 'no git'))"
echo "outdir $OUTDIR ${RESUME[*]:-}"

cd "$CODE_DIR"
# All scr_server slots must drive the same car (spec 0011): the image maps
# ports 3002-3010 to car1-trb1, our copy maps every port to car1-ow1.
SCR_XML=/usr/local/torcs/share/games/torcs/drivers/scr_server/scr_server.xml
# exec: SIGTERM from SLURM goes straight to apptainer -> train_cma.py
# (it stops the running evaluations and exits; checkpoint = last full generation).
exec apptainer exec --home "$HOME" --bind "$SCRATCH,$PROJECT" \
    --bind "$CODE_DIR/hpc/scr_server.xml:$SCR_XML:ro" "$SIF" \
    python3 -u train_cma.py --outdir "$OUTDIR" --workers "$WORKERS" --max-hours "$MAX_HOURS" \
    ${RESUME[@]+"${RESUME[@]}"} "$@"
