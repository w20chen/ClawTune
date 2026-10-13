#!/bin/bash
set -euo pipefail
cd /mnt/c/Users/29068/Desktop/ClawTune
export PATH=/home/user-of-wsl/.cache/clawtune-parser-venv/bin:$PATH
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=42
export PYTHONPATH=services/sidecar/src:.
mode=$1
family=$2
if [[ "$mode" == before* ]]; then export PYTHONPATH=.runtime/kb-algorithm-before:$PYTHONPATH; fi
out=research/kb_algorithm_acceleration_20261012/$mode
mkdir -p "$out"
if [ "$family" = flush ]; then
 python research/kb_algorithm_acceleration_20261012/flush_worker.py "$out" > "$out/flush.log" 2>&1
elif [ "$family" = edge ]; then
 python research/kb_algorithm_acceleration_20261012/edge_worker.py edge "$out" > "$out/edge.log" 2>&1
else
 python research/kb_algorithm_acceleration_20261012/lattice_worker.py lattice6 "$out" --prepare-update > "$out/lattice.log" 2>&1
fi
