#!/bin/bash
set -e
export PATH=/home/user-of-wsl/.cache/clawtune-parser-venv/bin:$PATH
cd /mnt/c/Users/29068/Desktop/ClawTune
export PYTHONPATH=services/sidecar/src:.
python -m pytest services/sidecar/tests tests -q -rs --basetemp /tmp/clawtune-lattice-p0-suite -o cache_dir=/tmp/clawtune-lattice-p0-cache > .runtime/lattice-p0/suite.log 2>&1