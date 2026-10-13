#!/bin/bash
set -euo pipefail
cd /mnt/c/Users/29068/Desktop/ClawTune
export PATH=/home/user-of-wsl/.cache/clawtune-parser-venv/bin:$PATH
export PYTHONDONTWRITEBYTECODE=1
export PYTHONHASHSEED=42
for mode in before after; do
 export PYTHONPATH=services/sidecar/src:.
 if [ "$mode" = before ]; then export PYTHONPATH=.runtime/kb-algorithm-before:$PYTHONPATH; fi
 python research/lattice_performance_20261012/synthetic_equivalence.py research/kb_algorithm_acceleration_20261012/synthetic-$mode.json
done
