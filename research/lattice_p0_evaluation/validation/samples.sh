#!/bin/bash
set -e
export PATH=/home/user-of-wsl/.cache/clawtune-parser-venv/bin:$PATH
cd /mnt/c/Users/29068/Desktop/ClawTune
export PYTHONPATH=services/sidecar/src:.
python .runtime/lattice-p0/inspect_samples.py > .runtime/lattice-p0/samples.log 2>&1