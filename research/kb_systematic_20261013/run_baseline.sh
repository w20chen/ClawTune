#!/bin/bash
set -euo pipefail
cd /home/weitianc/clawtune-kb-systematic-20261013
export PYTHONPATH=services/sidecar/src:.
export PYTHONHASHSEED=42
PY=/home/weitianc/ClawTune/.venv/bin/python
$PY -u research/kb_systematic_20261013/study.py prepare --dataset /home/weitianc/datasets/swe-rebench-original-flat-644-20260904
$PY -u research/kb_systematic_20261013/study.py frozen
