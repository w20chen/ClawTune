#!/usr/bin/env bash
set -euo pipefail
: "${PY:=python3}"
export PYTHONPATH=services/sidecar/src:.
export PYTHONHASHSEED=42
study=research/kb_systematic_20261013
mkdir -p "$study/logs"
"$PY" -m pytest \
  services/sidecar/tests/test_command_parser.py \
  services/sidecar/tests/test_command_projection.py \
  services/sidecar/tests/test_command_rule_generation.py \
  services/sidecar/tests/test_edge_kappa_kb.py \
  services/sidecar/tests/test_edge_kappa_runtime.py \
  services/sidecar/tests/test_lattice_query_policy.py \
  services/sidecar/tests/test_call_load.py \
  tests/test_edge_kappa_eval.py \
  tests/test_offline_bucket_summary.py \
  tests/test_offline_recorded_structure.py \
  "$study/test_protocol.py" \
  -q --basetemp "$study/pytest-validation" > "$study/logs/validation-final.log" 2>&1
CLAWTUNE_TEST_HELP_IMAGE=cube-sandbox-builder:ubuntu2004 "$PY" -m pytest \
  services/sidecar/tests/test_command_help_isolation.py \
  -q --basetemp "$study/pytest-help" > "$study/logs/validation-help-final.log" 2>&1
"$PY" -m ruff check services/sidecar/src/command_parser/generation.py \
  services/sidecar/tests/test_command_rule_generation.py \
  services/sidecar/tests/test_command_help_isolation.py > "$study/logs/validation-ruff-final.log" 2>&1
