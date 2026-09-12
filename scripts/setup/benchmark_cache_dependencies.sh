#!/usr/bin/env bash
# Optional preparation dependencies; no root Docker configuration is changed.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CACHE_HOME=${CLAWTUNE_CACHE_HOME:-"$HOME/clawtune-image-prepull"}
PYTHON=${CLAWTUNE_CACHE_PYTHON:-"$ROOT/.venv/bin/python"}
COMPOSE_VERSION=v5.5.1
case $(uname -m) in
  aarch64|arm64) ARCH=aarch64 ;;
  x86_64|amd64) ARCH=x86_64 ;;
  *) echo "Unsupported host architecture" >&2; exit 1 ;;
esac
"$PYTHON" -m pip install datasets 'httpx[socks]'
mkdir -p "$CACHE_HOME/bin" "$HOME/.docker/cli-plugins"
BASE="https://github.com/docker/compose/releases/download/$COMPOSE_VERSION"
curl -fL --retry 5 --connect-timeout 15 --max-time 300 \
  "$BASE/docker-compose-linux-$ARCH" -o "$CACHE_HOME/bin/docker-compose.download"
curl -fL --retry 5 --connect-timeout 15 --max-time 60 \
  "$BASE/checksums.txt" -o "$CACHE_HOME/compose-checksums.txt"
"$PYTHON" - "$CACHE_HOME" "$ARCH" <<'PY'
import hashlib
from pathlib import Path
import sys
root, arch = Path(sys.argv[1]), sys.argv[2]
expected = next(line.split()[0] for line in (root / 'compose-checksums.txt').read_text().splitlines()
                if line.split()[-1].lstrip('*') == 'docker-compose-linux-' + arch)
download = root / 'bin/docker-compose.download'
if hashlib.sha256(download.read_bytes()).hexdigest() != expected:
    raise SystemExit('Compose checksum mismatch')
download.chmod(0o755)
download.replace(root / 'bin/docker-compose')
PY
ln -sfn "$CACHE_HOME/bin/docker-compose" "$HOME/.docker/cli-plugins/docker-compose"
"$CACHE_HOME/bin/docker-compose" version
