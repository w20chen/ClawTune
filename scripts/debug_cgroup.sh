#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "ClawTune .venv is missing; run: python3 scripts/clawtune.py setup" >&2
  exit 2
fi

KERNEL_RELEASE="$(uname -r)"
KERNEL_BUILD="$(readlink -f "/lib/modules/${KERNEL_RELEASE}/build" 2>/dev/null || true)"
if [[ -z "${KERNEL_BUILD}" || ! -d "${KERNEL_BUILD}" ]]; then
  echo "Kernel headers are missing for ${KERNEL_RELEASE}; run ClawTune setup first." >&2
  exit 2
fi

exec sudo env \
  "PATH=${PATH}" \
  "PYTHONPATH=${REPO_ROOT}/services/sidecar/src" \
  "BCC_KERNEL_SOURCE=${KERNEL_BUILD}" \
  "${VENV_PYTHON}" "${SCRIPT_DIR}/debug_cgroup.py" "$@"
