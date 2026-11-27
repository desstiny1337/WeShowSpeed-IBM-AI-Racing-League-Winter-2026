#!/bin/bash
# Install numpy + cma (pycma) for train_cma.py into a shared dir (spec 0010).
# The apptainer image (Python 3.10, no pip) uses them via PYTHONPATH.
# Run on the PERUN login node (host python3 3.9 with pip is enough: only
# binary wheels for cp310 / manylinux2014 are downloaded, nothing is built).
#
#   PERUN_PROJECT=<project> bash hpc/setup_pylib.sh [target_dir]
#
# PERUN_PROJECT is the project/account id (see hpc/README.md); it is only
# needed when target_dir or SIF are not given.

set -euo pipefail

PROJECT=/mnt/project/${PERUN_PROJECT:-}
PYLIB=${1:-$PROJECT/torcs/pylib}
SIF=${SIF:-$PROJECT/torcs/torcs-env.sif}
if [ -z "${PERUN_PROJECT:-}" ] && { [ -z "${1:-}" ] || [ ! -f "$SIF" ]; }; then
    echo "set PERUN_PROJECT (or pass target_dir and SIF)" >&2
    exit 2
fi

mkdir -p "$PYLIB"
python3 -m pip install --upgrade --only-binary=:all: \
    --python-version 3.10 --platform manylinux2014_x86_64 --implementation cp \
    --target "$PYLIB" numpy==2.0.2 cma==4.6.0

# Check inside the image (skipped if apptainer is not on PATH).
if command -v apptainer >/dev/null 2>&1; then
    PYTHONPATH=$PYLIB apptainer exec --bind "$PYLIB" "$SIF" \
        python3 -c "import sys, numpy, cma; print(sys.version.split()[0], 'numpy', numpy.__version__, 'cma', cma.__version__)"
fi
