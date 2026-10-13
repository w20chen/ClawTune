#!/bin/bash
set -euo pipefail
cd /home/weitianc/clawtune-kb-systematic-20261013
export PYTHONPATH=services/sidecar/src:.
export PYTHONHASHSEED=42
PY=/home/weitianc/ClawTune/.venv/bin/python
$PY -u -m research.kb_systematic_20261013.optimization train
$PY -u -m research.kb_systematic_20261013.optimization test
