#!/usr/bin/env bash
# Run from a fresh study root after unzipping final-source.zip and baseline.zip.
# Example:
# PY=/home/weitianc/ClawTune/.venv/bin/python DATASET=/path/to/644-traces \
# SAVED_RESULTS=/path/to/delivered/results bash research/kb_systematic_20261013/reproduce.sh
set -euo pipefail
: "${DATASET:?Set the original read-only dataset path}"
: "${PY:=python3}"
export PYTHONPATH=services/sidecar/src:.
export PYTHONHASHSEED=42
study=research/kb_systematic_20261013
out="$study/results"
if [[ -e "$out" ]]; then
  printf '%s\n' 'Use a fresh extraction; results already exists.' >&2
  exit 1
fi
test -f baseline/original-split.json
mkdir -p "$study/logs"
# Explicit commands retain meaningful names/arguments in the logs.
"$PY" -u -m research.kb_systematic_20261013.study prepare --dataset "$DATASET" > "$study/logs/prepare.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.optimization events > "$study/logs/events.log" 2>&1
"$PY" -m research.kb_systematic_20261013.optimization isolate
if [[ "${LIVE_LLM:-0}" == 1 ]]; then
  : "${CLAWTUNE_STUDY_KEY_FILE:?Set the API key file path; never paste the key into commands}"
  export CLAWTUNE_STUDY_KEY_FILE
  "$PY" -u -m research.kb_systematic_20261013.optimization semantic > "$study/logs/semantic.log" 2>&1
else
  : "${SAVED_RESULTS:?Set the delivered results directory to replay saved LLM outputs}"
  cp "$SAVED_RESULTS/semantic-prior.json" "$out/semantic-prior.json"
  cp -a "$SAVED_RESULTS/llm" "$out/llm"
fi
"$PY" -u -m research.kb_systematic_20261013.study frozen > "$study/logs/frozen.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.optimization train > "$study/logs/train.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.optimization test > "$study/logs/test.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.transfer transfer > "$study/logs/transfer.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.transfer cold > "$study/logs/cold.log" 2>&1
if [[ "${LIVE_LLM:-0}" == 1 ]]; then
  # Requires an already pulled cube-sandbox-builder:ubuntu2004 image with tool help.
  "$PY" -u -m research.kb_systematic_20261013.llm_rules generate > "$study/logs/rules.log" 2>&1
fi
"$PY" -u -m research.kb_systematic_20261013.llm_rules evaluate > "$study/logs/rules-evaluate.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.verify > "$study/logs/verify.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.audit ablations > "$study/logs/ablations.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.audit worst > "$study/logs/worst.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.interfaces > "$study/logs/interfaces.log" 2>&1
"$PY" -u -m research.kb_systematic_20261013.legacy_interfaces > "$study/logs/legacy-interfaces.log" 2>&1
"$PY" -m research.kb_systematic_20261013.analysis
"$PY" -m research.kb_systematic_20261013.extra_analysis
"$PY" -m research.kb_systematic_20261013.final_analysis
# report.md describes the delivered run. Review new metrics before writing a new narrative,
# especially when LIVE_LLM=1 returns different rules or provider versions.
"$PY" -m pytest services/sidecar/tests/test_command_rule_generation.py "$study/test_protocol.py" -q --basetemp "$study/pytest-tmp"
# Optional figure rendering: install matplotlib in a separate plot environment.
# PYTHONPATH=plot-deps:services/sidecar/src:. "$PY" -m research.kb_systematic_20261013.plots
