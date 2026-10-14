#!/bin/sh
set -eu
cd /mnt/c/Users/29068/Desktop/ClawTune
export PYTHONHASHSEED=42
export PYTHONUNBUFFERED=1
exec .runtime/wsl-offline-venv2/bin/python -u research/lattice_worst30_20261014/run.py > research/lattice_worst30_20261014/run.log 2>&1
