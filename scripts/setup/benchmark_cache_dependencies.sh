#!/usr/bin/env bash
# Optional preparation dependencies; no root Docker configuration is changed.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PYTHON=${CLAWTUNE_CACHE_PYTHON:-"$ROOT/.venv/bin/python"}
"$PYTHON" -m pip install datasets 'httpx[socks]'
"$PYTHON" "$ROOT/scripts/setup/docker_tools.py" install
