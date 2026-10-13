#!/bin/bash
set -e
export PATH=/home/user-of-wsl/.cache/clawtune-parser-venv/bin:$PATH
cd /mnt/c/Users/29068/Desktop/ClawTune
export PYTHONPATH=services/sidecar/src:.
python -m benchmarks.cli offline --dataset /mnt/c/Users/29068/Desktop/swe-rebench-original-flat-644-20260904/swe-rebench-original-flat-644-20260904 --benchmark swe-rebench --rss-unit MiB --seed 42 --train-fraction .8 --split-cache-dir .runtime/command-rules/splits --output .runtime/lattice-p0/after > .runtime/lattice-p0/after.log 2>&1