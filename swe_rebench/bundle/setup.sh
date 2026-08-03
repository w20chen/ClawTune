#!/bin/bash
# ────────────────────────────────────────────────────────────────
# Environment setup inside swe-rebench containers.
# Installs Node.js, npm, OpenClaw CLI, and Python deps.
# Idempotent -- safe to run multiple times.
#
# Architecture support: x86_64 (amd64), aarch64 (arm64 / Kunpeng).
# BCC/eBPF deps are best-effort and fail-open on all architectures.
# ────────────────────────────────────────────────────────────────
set -euo pipefail

echo "[claw] container arch: $(uname -m)"

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

CLAW_ROOT="${CLAW_ROOT:-/claw}"
SETUP_DONE="/tmp/.claw_setup_done"
SETUP_REVISION="2:mvdan-protocol-3:mvdan-v3.13.1"
MVDAN_STATUS="/tmp/.claw_mvdan_adapter_status.json"

_claw_mvdan_adapter_ready() {
    env "PYTHONPATH=$CLAW_ROOT/scheduler/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$_CLW_PYTHON" -c \
        'from tool_resource.mvdan_client import MvdanClient, default_binary_path; client = MvdanClient(default_binary_path()); client.__enter__(); client.close()' \
        >/dev/null 2>&1
}

if [ -f "$SETUP_DONE" ] \
    && [ "$(cat "$SETUP_DONE" 2>/dev/null || true)" = "$SETUP_REVISION" ] \
    && _claw_mvdan_adapter_ready
then
    echo "[claw] setup already complete, skipping."
    return 0 2>/dev/null || exit 0
fi
if [ -f "$SETUP_DONE" ]; then
    echo "[claw] stale setup marker or mvdan adapter; rerunning setup."
fi

echo "[claw] checking container system dependencies..."

# ── Detect package manager ──────────────────────────────────────
if command -v apt-get &>/dev/null; then PKG_MGR="apt"
elif command -v yum &>/dev/null; then PKG_MGR="yum"
elif command -v dnf &>/dev/null; then PKG_MGR="dnf"
elif command -v apk &>/dev/null; then PKG_MGR="apk"
else PKG_MGR="none"
fi

echo "[claw] package manager: $PKG_MGR"

_claw_run_bounded() {
    if command -v timeout &>/dev/null; then
        timeout --signal=TERM "${CLAW_SETUP_COMMAND_TIMEOUT_SECONDS:-300}" "$@"
    else
        "$@"
    fi
}

_claw_apt() {
    DEBIAN_FRONTEND=noninteractive _claw_run_bounded apt-get \
        -o Acquire::Retries=2 \
        -o Acquire::http::Timeout=20 \
        -o Acquire::https::Timeout=20 \
        "$@"
}

case "$PKG_MGR" in
    apt)
        echo "[claw] refreshing apt metadata (network timeout: 20s, retries: 2)..."
        if ! _claw_apt update; then
            echo "[claw] FATAL: apt metadata refresh failed; check container DNS/proxy/mirror access"
            exit 1
        fi
        echo "[claw] apt metadata ready"
        ;;
    apk) apk update ;;
esac

# ── curl (needed for health checks + nodesource) ────────────────
if ! command -v curl &>/dev/null; then
    echo "[claw] installing curl..."
    case "$PKG_MGR" in
        apt) _claw_apt install -y -q curl ;;
        yum) yum install -y -q curl ;;
        dnf) dnf install -y -q curl ;;
        apk) apk add --no-cache curl ;;
    esac
fi

# ── Docker CLI (needed by sidecar DockerExecObserver) ───────────
if ! command -v docker &>/dev/null; then
    echo "[claw] installing docker CLI..."
    case "$PKG_MGR" in
        apt) _claw_apt install -y -q docker.io || _claw_apt install -y -q docker-ce-cli || true ;;
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

echo "[claw] installing BCC/eBPF dependencies (best-effort)..."
case "$PKG_MGR" in
    # libbpfcc's Python extension dynamically links libelf.so.1.  Keep this
    # container-only bootstrap fail-open, but install the runtime library
    # explicitly: minimal task images do not always pull it transitively.
    apt) _claw_apt install -y -q python3-bpfcc bpfcc-tools libbpfcc libelf1 || true ;;
    yum) yum install -y -q bcc-tools python3-bcc 2>/dev/null || true ;;
    dnf) dnf install -y -q bcc-tools python3-bcc 2>/dev/null || true ;;
    apk) apk add --no-cache bcc-tools bcc-python3 2>/dev/null || true ;;
esac
case "$PKG_MGR" in
    apt)
        _claw_apt install -y -q clang llvm kmod linux-headers-"$(uname -r)" \
            || _claw_apt install -y -q clang llvm kmod linux-headers-generic \
            || _claw_apt install -y -q clang llvm kmod \
            || true
        ;;
    yum) yum install -y -q clang llvm kmod kernel-headers kernel-devel 2>/dev/null || yum install -y -q clang llvm kmod 2>/dev/null || true ;;
    dnf) dnf install -y -q clang llvm kmod kernel-headers kernel-devel 2>/dev/null || dnf install -y -q clang llvm kmod 2>/dev/null || true ;;
    apk) apk add --no-cache clang llvm kmod linux-headers 2>/dev/null || true ;;
esac
if [ -d /usr/lib/python3/dist-packages/bcc ]; then
    echo "/usr/lib/python3/dist-packages" > /tmp/.claw_bcc_pythonpath
else
    _CLAW_BCC_PATH="$(find /usr/lib /usr/lib64 -path '*/site-packages/bcc' -type d -print -quit 2>/dev/null || true)"
    if [ -n "$_CLAW_BCC_PATH" ]; then
        dirname "$_CLAW_BCC_PATH" > /tmp/.claw_bcc_pythonpath
    fi
fi

# Some minimized benchmark images retain dpkg's "installed" record for
# libelf1 after removing its shared-object payload.  Conda-based images can
# also force an older libstdc++.so.6 into the selected Python even though BCC
# and libclang were installed from the system package manager.  Repair only
# those observed container failures; all other optional BCC failures remain
# fail-open.
_CLAW_BCC_PYTHONPATH=""
_CLAW_BCC_LD_PRELOAD=""
_CLAW_BCC_PRELOAD_FILE="/tmp/.claw_bcc_ld_preload"
# SETUP_DONE returns before this point, so a completed setup keeps its verified
# marker while a fresh or resumed setup cannot inherit a stale one.
rm -f -- "$_CLAW_BCC_PRELOAD_FILE" || true

_claw_import_bcc() {
    if [ -n "$_CLAW_BCC_LD_PRELOAD" ]; then
        LD_PRELOAD="${_CLAW_BCC_LD_PRELOAD}${LD_PRELOAD:+:$LD_PRELOAD}" \
        PYTHONPATH="${_CLAW_BCC_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}" \
            "$_CLW_PYTHON" -c "import bcc"
    else
        PYTHONPATH="${_CLAW_BCC_PYTHONPATH}${PYTHONPATH:+:$PYTHONPATH}" \
            "$_CLW_PYTHON" -c "import bcc"
    fi
}

_claw_try_system_libstdcxx() {
    local candidate
    local candidate_error=""

    if ! command -v ldconfig &>/dev/null; then
        return 1
    fi
    while IFS= read -r candidate; do
        [ -r "$candidate" ] || continue
        case "$candidate" in
            /lib/*|/lib64/*|/usr/lib/*|/usr/lib64/*) ;;
            *) continue ;;
        esac
        _CLAW_BCC_LD_PRELOAD="$candidate"
        if candidate_error="$(_claw_import_bcc 2>&1)"; then
            if printf '%s\n' "$candidate" > "$_CLAW_BCC_PRELOAD_FILE" \
                && chmod 0600 "$_CLAW_BCC_PRELOAD_FILE"
            then
                echo "[claw] BCC runtime repaired with system libstdc++ (sidecar-only): $candidate"
                return 0
            fi
            candidate_error="verified $candidate but could not persist the sidecar-only preload marker"
            rm -f "$_CLAW_BCC_PRELOAD_FILE" || true
        fi
        _CLAW_BCC_LD_PRELOAD=""
        _CLAW_BCC_PRELOAD_ERROR="$candidate_error"
    done < <(
        ldconfig -p 2>/dev/null \
            | awk '$1 == "libstdc++.so.6" && !seen[$NF]++ { print $NF }'
    )
    return 1
}

if [ -s /tmp/.claw_bcc_pythonpath ]; then
    _CLAW_BCC_PYTHONPATH="$(cat /tmp/.claw_bcc_pythonpath)"
    _CLAW_BCC_IMPORT_ERROR=""
    if ! _CLAW_BCC_IMPORT_ERROR="$(_claw_import_bcc 2>&1)"; then
        if [ "$PKG_MGR" = "apt" ]; then
            case "$_CLAW_BCC_IMPORT_ERROR" in
                *"libelf.so.1"*)
                    echo "[claw] libelf.so.1 is missing; reinstalling libelf1..."
                    if _claw_apt install -y -q --reinstall libelf1; then
                        if command -v ldconfig &>/dev/null; then
                            ldconfig 2>/dev/null || true
                        fi
                        if _CLAW_BCC_RECHECK_ERROR="$(_claw_import_bcc 2>&1)"; then
                            _CLAW_BCC_IMPORT_ERROR=""
                            echo "[claw] libelf1 reinstall repaired the BCC runtime"
                        else
                            _CLAW_BCC_IMPORT_ERROR="$_CLAW_BCC_RECHECK_ERROR"
                        fi
                    else
                        echo "[claw] libelf1 reinstall failed (Stage-2 will remain unavailable)"
                    fi
                    ;;
            esac
        fi
        if [ -n "$_CLAW_BCC_IMPORT_ERROR" ]; then
            case "$_CLAW_BCC_IMPORT_ERROR" in
                *"libstdc++.so.6"*GLIBCXX_*"not found"*)
                    echo "[claw] Conda libstdc++ is incompatible with system BCC; probing a sidecar-only system preload..."
                    if _claw_try_system_libstdcxx; then
                        _CLAW_BCC_IMPORT_ERROR=""
                    else
                        _CLAW_BCC_IMPORT_ERROR="${_CLAW_BCC_PRELOAD_ERROR:-$_CLAW_BCC_IMPORT_ERROR}"
                    fi
                    ;;
            esac
        fi
        if [ -n "$_CLAW_BCC_IMPORT_ERROR" ]; then
            echo "[claw] BCC remains unavailable after container repair probes: $_CLAW_BCC_IMPORT_ERROR"
        fi
    fi
fi

# ── Python 3 (system fallback -- usually conda is already present) ──
if ! $_CLW_PYTHON --version &>/dev/null 2>&1; then
    echo "[claw] installing python3..."
    case "$PKG_MGR" in
        apt) _claw_apt install -y -q python3 python3-pip ;;
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
    # Resolve the current Node.js 24 archive instead of pinning a patch release
    # that disappears from the latest-v24.x alias. Select the detected
    # architecture from the checksum manifest so ARM does not fall back to x64.
    NODE_BASE_URL="https://nodejs.org/dist/latest-v24.x"
    if ! NODE_SHASUMS="$(curl -fsSL --connect-timeout 15 --max-time 60 "$NODE_BASE_URL/SHASUMS256.txt")"; then
        echo "[claw] FATAL: cannot resolve the latest Node.js 24 release"
        exit 1
    fi
    LATEST="$(
        printf '%s\n' "$NODE_SHASUMS" \
            | awk -v arch="$NODE_ARCH" '$2 ~ ("-linux-" arch "\\.tar\\.xz$") { print $2; exit }'
    )"
    if [ -z "$LATEST" ]; then
        echo "[claw] FATAL: no Node.js 24 archive for architecture $NODE_ARCH"
        exit 1
    fi
    if ! curl -fsSL --connect-timeout 15 --max-time 180 \
        "$NODE_BASE_URL/$LATEST" -o "/tmp/node.tar.xz"; then
        echo "[claw] FATAL: Node.js download failed; check container DNS/proxy/network access"
        exit 1
    fi
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
    _claw_run_bounded env \
        npm_config_fetch_retries=2 \
        npm_config_fetch_timeout=120000 \
        npm install -g openclaw@2026.7.1 || \
    _claw_run_bounded env \
        npm_config_fetch_retries=2 \
        npm_config_fetch_timeout=120000 \
        npm install -g openclaw || {
        echo "[claw] FATAL: openclaw install failed"
        exit 1
    }
fi
echo "[claw] openclaw $(openclaw --version 2>&1 | head -1)"

# ── Sidecar Python deps ─────────────────────────────────────────
echo "[claw] installing sidecar Python deps..."
$_CLW_PIP install --quiet \
    fastapi uvicorn pydantic psutil httpx prometheus-client numpy typing-extensions \
    2>&1 | tail -1
if [ -s /tmp/.claw_bcc_pythonpath ]; then
    export PYTHONPATH="$(cat /tmp/.claw_bcc_pythonpath)${PYTHONPATH:+:$PYTHONPATH}"
fi
_CLAW_BCC_RUNTIME_ENV=()
if [ -s "$_CLAW_BCC_PRELOAD_FILE" ]; then
    IFS= read -r _CLAW_BCC_LD_PRELOAD < "$_CLAW_BCC_PRELOAD_FILE" \
        || _CLAW_BCC_LD_PRELOAD=""
    case "$_CLAW_BCC_LD_PRELOAD" in
        /lib/*|/lib64/*|/usr/lib/*|/usr/lib64/*)
            if [ -r "$_CLAW_BCC_LD_PRELOAD" ]; then
                _CLAW_BCC_RUNTIME_ENV=(
                    "LD_PRELOAD=${_CLAW_BCC_LD_PRELOAD}${LD_PRELOAD:+:$LD_PRELOAD}"
                )
            else
                _CLAW_BCC_LD_PRELOAD=""
            fi
            ;;
        *) _CLAW_BCC_LD_PRELOAD="" ;;
    esac
fi
if [ -n "$_CLAW_BCC_LD_PRELOAD" ]; then
    if ! env "${_CLAW_BCC_RUNTIME_ENV[@]}" "$_CLW_PYTHON" \
        -c "import fastapi, uvicorn, pydantic, psutil, numpy, typing_extensions, bcc; print('[claw] sidecar deps and BCC OK with system libstdc++')"
    then
        echo "[claw] system libstdc++ preload failed the combined sidecar/BCC probe; disabling the Stage-2 preload"
        rm -f "$_CLAW_BCC_PRELOAD_FILE" || true
        _CLAW_BCC_LD_PRELOAD=""
        _CLAW_BCC_RUNTIME_ENV=()
        "$_CLW_PYTHON" -c "import fastapi, uvicorn, pydantic, psutil, numpy, typing_extensions; print('[claw] sidecar deps OK')"
    fi
else
    "$_CLW_PYTHON" -c "import fastapi, uvicorn, pydantic, psutil, numpy, typing_extensions; print('[claw] sidecar deps OK')"
fi
env "${_CLAW_BCC_RUNTIME_ENV[@]}" "$_CLW_PYTHON" - <<'PY' || true
try:
    import bcc  # noqa: F401
    print("[claw] BCC Python binding OK")
except Exception as exc:
    print(f"[claw] BCC Python binding unavailable: {type(exc).__name__}: {exc}")
PY

# ── Done ────────────────────────────────────────────────────────
echo "[claw] building/verifying pinned mvdan adapter..."
if env "PYTHONPATH=$CLAW_ROOT/scheduler/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$_CLW_PYTHON" - "$MVDAN_STATUS" <<'PY'
import json
import os
import sys
from pathlib import Path

from tool_resource.mvdan_client import (
    ADAPTER_PROTOCOL_VERSION,
    PARSER_NAME,
    PARSER_VERSION,
    REQUIRED_CAPABILITIES,
    MvdanClient,
    default_binary_path,
    ensure_compatible_adapter,
)

status_path = Path(sys.argv[1])
binary_path = default_binary_path()
status = {
    "attempted": True,
    "ok": False,
    "binary_path": str(binary_path),
    "cache_present_before": binary_path.is_file(),
    "parser": {"name": PARSER_NAME, "version": PARSER_VERSION},
    "protocol": {
        "version": ADAPTER_PROTOCOL_VERSION,
        "required_capabilities": sorted(REQUIRED_CAPABILITIES),
    },
    "error": None,
}
try:
    built_path = ensure_compatible_adapter()
    with MvdanClient(built_path):
        pass
    status.update(
        {
            "ok": True,
            "binary_path": str(built_path),
            "binary_exists": built_path.is_file(),
            "binary_executable": os.access(built_path, os.X_OK),
        }
    )
except Exception as exc:
    status["error"] = f"{type(exc).__name__}: {exc}"

status_path.parent.mkdir(parents=True, exist_ok=True)
temporary_path = status_path.with_name(
    f"{status_path.name}.{os.getpid()}.tmp"
)
temporary_path.write_text(
    json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
os.chmod(temporary_path, 0o600)
os.replace(temporary_path, status_path)
if not status["ok"]:
    raise SystemExit(1)
print(
    "[claw] mvdan adapter OK "
    f"({PARSER_NAME} {PARSER_VERSION}, protocol {ADAPTER_PROTOCOL_VERSION})"
)
PY
then
    printf '%s\n' "$SETUP_REVISION" > "$SETUP_DONE.$$"
    chmod 0600 "$SETUP_DONE.$$"
    mv -f "$SETUP_DONE.$$" "$SETUP_DONE"
else
    rm -f -- "$SETUP_DONE"
    echo "[claw] mvdan adapter unavailable (Stage-2 will remain unavailable)"
fi

echo "[claw] setup complete."
