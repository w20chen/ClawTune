#!/bin/bash
# ────────────────────────────────────────────────────────────────
# Environment setup inside swe-rebench containers.
# Installs Node.js, npm, OpenClaw CLI, and Python deps.
# Idempotent -- safe to run multiple times.
# ────────────────────────────────────────────────────────────────
set -euo pipefail

SETUP_DONE="/tmp/.claw_setup_done"
if [ -f "$SETUP_DONE" ]; then
    echo "[claw] setup already complete, skipping."
    return 0 2>/dev/null || exit 0
fi

# Detect python: prefer conda python shipped by swe-rebench images.
if [ -x /opt/conda/bin/python3 ]; then
    _CLW_PYTHON="/opt/conda/bin/python3"
    _CLW_PIP="/opt/conda/bin/pip"
elif command -v python3 &>/dev/null; then
    _CLW_PYTHON="$(command -v python3)"
    _CLW_PIP="$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null)"
else
    _CLW_PYTHON="python3"
    _CLW_PIP="pip3"
fi


echo "[claw] installing system dependencies..."

# ── Detect package manager ──────────────────────────────────────
if command -v apt-get &>/dev/null; then PKG_MGR="apt"
elif command -v yum &>/dev/null; then PKG_MGR="yum"
elif command -v dnf &>/dev/null; then PKG_MGR="dnf"
elif command -v apk &>/dev/null; then PKG_MGR="apk"
else PKG_MGR="none"
fi

case "$PKG_MGR" in
    apt) apt-get update -qq ;;
    apk) apk update ;;
esac

# ── curl (needed for health checks + nodesource) ────────────────
if ! command -v curl &>/dev/null; then
    echo "[claw] installing curl..."
    case "$PKG_MGR" in
        apt) apt-get install -y -qq curl ;;
        yum) yum install -y -q curl ;;
        dnf) dnf install -y -q curl ;;
        apk) apk add --no-cache curl ;;
    esac
fi

# ── Docker CLI (needed by sidecar DockerExecObserver) ───────────
if ! command -v docker &>/dev/null; then
    echo "[claw] installing docker CLI..."
    case "$PKG_MGR" in
        apt) apt-get install -y -qq docker.io 2>/dev/null || apt-get install -y -qq docker-ce-cli 2>/dev/null || true ;;
        yum) yum install -y -q docker-cli 2>/dev/null || yum install -y -q docker 2>/dev/null || true ;;
        dnf) dnf install -y -q docker-cli 2>/dev/null || dnf install -y -q docker 2>/dev/null || true ;;
        apk) apk add --no-cache docker-cli 2>/dev/null || apk add --no-cache docker 2>/dev/null || true ;;
    esac
fi
if command -v docker &>/dev/null; then
    echo "[claw] docker CLI OK"
else
    echo "[claw] docker CLI not available (DockerExecObserver will idle)"
fi

# ── Python 3 (system fallback -- usually conda is already present) ──
if ! $_CLW_PYTHON --version &>/dev/null 2>&1; then
    echo "[claw] installing python3..."
    case "$PKG_MGR" in
        apt) apt-get install -y -qq python3 python3-pip ;;
        yum) yum install -y -q python3 python3-pip ;;
        dnf) dnf install -y -q python3 python3-pip ;;
        apk) apk add --no-cache python3 py3-pip ;;
        *)  echo "[claw] FATAL: cannot install python3" ; exit 1 ;;
    esac
    _CLW_PYTHON="$(command -v python3)"
    _CLW_PIP="$(command -v pip3 2>/dev/null || command -v pip 2>/dev/null)"
fi
echo "[claw] python=$_CLW_PYTHON"

# ── Node.js 24 (direct tarball, no gpg needed) ──────────────────
NODE_OK=0
if command -v node &>/dev/null && node --version &>/dev/null 2>&1; then
    NODE_OK=1
fi
if [ "$NODE_OK" -eq 0 ]; then
    echo "[claw] installing Node.js (direct download)..."
    NODE_ARCH="x64"
    case "$(uname -m)" in
        aarch64|arm64) NODE_ARCH="arm64" ;;
    esac
    # Download the latest Node.js 24 LTS
    NODE_URL="https://nodejs.org/dist/latest-v24.x/node-v24.15.0-linux-${NODE_ARCH}.tar.xz"
    curl -fsSL "$NODE_URL" -o "/tmp/node.tar.xz" || {
        # Fallback: try without specific patch version
        NODE_URL="https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt"
        LATEST=$(curl -fsSL "https://nodejs.org/dist/latest-v24.x/" 2>/dev/null | grep -oP 'node-v24\.[0-9]+\.[0-9]+-linux-x64\.tar\.xz' | head -1)
        if [ -n "$LATEST" ]; then
            curl -fsSL "https://nodejs.org/dist/latest-v24.x/${LATEST}" -o "/tmp/node.tar.xz"
        else
            echo "[claw] FATAL: cannot download Node.js"
            exit 1
        fi
    }
    tar -xJf "/tmp/node.tar.xz" -C /usr/local --strip-components=1
    rm -f "/tmp/node.tar.xz"
fi

# Verify node actually works
if node --version &>/dev/null 2>&1; then
    echo "[claw] node $(node --version) OK"
else
    echo "[claw] FATAL: node installed but does not run"
    ldd "$(command -v node)" 2>&1 | grep "not found" || true
    exit 1
fi

# ── OpenClaw CLI ─────────────────────────────────────────────────
if ! command -v openclaw &>/dev/null; then
    echo "[claw] installing openclaw CLI..."
    npm install -g openclaw@2026.7.1 2>/dev/null || npm install -g openclaw 2>/dev/null || {
        echo "[claw] FATAL: openclaw install failed"
        exit 1
    }
fi
echo "[claw] openclaw $(openclaw --version 2>&1 | head -1)"

# ── Sidecar Python deps ─────────────────────────────────────────
echo "[claw] installing sidecar Python deps..."
$_CLW_PIP install --quiet \
    fastapi uvicorn pydantic psutil httpx prometheus-client \
    2>&1 | tail -1
$_CLW_PYTHON -c "import fastapi, uvicorn, pydantic, psutil; print('[claw] sidecar deps OK')"

# ── Done ────────────────────────────────────────────────────────
touch "$SETUP_DONE"
echo "[claw] setup complete."
