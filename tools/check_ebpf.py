"""Compile, attach, and semantically validate ClawTune eBPF telemetry."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

def _load_telemetry() -> ModuleType:
    sidecar_src = REPO_ROOT / "services" / "sidecar" / "src"
    source = str(sidecar_src)
    if source not in sys.path:
        sys.path.insert(0, source)
    return importlib.import_module("tool_resource.telemetry")


def run_preflight(telemetry: ModuleType | None = None) -> dict[str, Any]:
    """Return the detailed, JSON-serializable eBPF preflight report."""

    report: dict[str, Any] = {
        "schema": "clawtune.ebpf-preflight.v1",
        "platform": platform.system().lower(),
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "python": sys.executable,
        "docker": shutil.which("docker"),
        "bcc_kernel_source": os.getenv("BCC_KERNEL_SOURCE"),
        "ebpf_ready": False,
    }
    try:
        module = telemetry or _load_telemetry()
        diagnostics = module._bpf_runtime_diagnostics()
        report["bpf_runtime"] = diagnostics

        bcc = module._ensure_bcc_importable()
        report["bcc_import"] = {
            "ok": True,
            "module": getattr(bcc, "__name__", None),
            "path": getattr(bcc, "__file__", None),
        }

        header_roots = list(diagnostics.get("kernel_headers") or [])
        configured_source = os.getenv("BCC_KERNEL_SOURCE")
        if configured_source and Path(configured_source).is_dir():
            header_roots.append(configured_source)
        report["kernel_header_roots"] = sorted(set(header_roots))
        if not header_roots:
            raise RuntimeError("no matching running-kernel build/header tree was found")
        if report["docker"] is None:
            raise RuntimeError("docker is not available on PATH")

        module.validate_clause_telemetry_runtime(
            container_executable="docker",
            concurrency=1,
            workers=1,
        )
        report["semantic_smoke"] = module.validate_clause_telemetry_smoke()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    else:
        report["ebpf_ready"] = True
        report["error"] = None
    return report


def run_check() -> dict[str, Any]:
    internal = run_preflight()
    return {
        "ready": internal.get("ebpf_ready") is True,
        "platform": internal.get("platform"),
        "python": internal.get("python"),
        "bcc": internal.get("bcc_import"),
        "kernel_headers": internal.get("kernel_header_roots", []),
        "runtime": internal.get("bpf_runtime"),
        "semantic_smoke": internal.get("semantic_smoke"),
        "error": internal.get("error"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that ClawTune can compile, attach, and use its eBPF collector.",
    )
    parser.add_argument("--output", type=Path, help="Also write the JSON report here.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_check()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
