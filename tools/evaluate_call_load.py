"""Score already-recorded held-out call predictions; never modify source data."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "sidecar" / "src"))

from clawtune_sidecar.predictors.call_load_eval import evaluate_calls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("records", type=Path, help="JSONL of prediction, actual, scope and lifecycle")
    args = parser.parse_args()
    with args.records.open(encoding="utf-8") as source:
        report = evaluate_calls(json.loads(line) for line in source if line.strip())
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
