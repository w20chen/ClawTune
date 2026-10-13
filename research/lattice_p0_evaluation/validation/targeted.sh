#!/bin/bash
set -e
export PATH=/home/user-of-wsl/.cache/clawtune-parser-venv/bin:$PATH
cd /mnt/c/Users/29068/Desktop/ClawTune
export PYTHONPATH=services/sidecar/src:.
python -m pytest services/sidecar/tests/test_lattice_query_policy.py services/sidecar/tests/test_lattice_time_kb.py services/sidecar/tests/test_lattice_resources.py -q --basetemp /tmp/clawtune-p0-tests -o cache_dir=/tmp/clawtune-p0-cache > .runtime/lattice-p0/targeted.log 2>&1