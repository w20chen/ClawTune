"""Run the strict host-side Stage-2 eBPF semantic preflight.

This command is intentionally separate from Scheduler startup: operators can
prove that the selected Python/BCC/kernel/cgroup combination works before
starting OpenClaw or a benchmark task.
"""

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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_telemetry() -> ModuleType:
    scheduler_src = _repo_root() / "services" / "scheduler" / "src"
    source = str(scheduler_src)
    if source not in sys.path:
        sys.path.insert(0, source)
    return importlib.import_module("tool_resource.telemetry")


def run_preflight(telemetry: ModuleType | None = None) -> dict[str, Any]:
    """Return a JSON-serializable strict preflight report."""

    report: dict[str, Any] = {
        "schema": "clawtune_stage2_preflight_v1",
        "platform": platform.system().lower(),
        "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        "python": sys.executable,
        "docker": shutil.which("docker"),
        "bcc_kernel_source": os.getenv("BCC_KERNEL_SOURCE"),
        "stage2_ready": False,
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
        report["stage2_ready"] = True
        report["error"] = None
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile, attach, and semantically validate ClawTune Stage-2 eBPF telemetry.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Also write the JSON report to this path.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_preflight()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if report["stage2_ready"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
