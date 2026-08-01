"""User-facing eBPF readiness check.

The collector keeps its historical internal field names for protocol
compatibility.  This command presents a small, stable report to operators.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.check_stage2 import run_preflight


def run_check() -> dict[str, Any]:
    internal = run_preflight()
    return {
        "ready": internal.get("stage2_ready") is True,
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
