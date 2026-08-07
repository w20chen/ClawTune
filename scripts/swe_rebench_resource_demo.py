"""Temporary demo: predict per-tool memory / cpu / disk / network on
``agent_datasets/swe-rebench`` using the existing RuntimeToolResourceKB
conditional-p90 machinery.

The dataset is tool-level: ``tool_calls.json`` (tool, command, duration_ms) and
``resources.json`` (timestamped container resource samples: memory / cpu /
net_rx|tx / disk_read|write).  There is no per-tool resource field, so each
tool call is attributed the resource deltas / peak values over its time window.

Reuses existing pieces (no project code modified):
  - legacy_eval.split.split_observations_by_repo / repo_prefix (per-repo split)
  - tool_resource.runtime_kb.RuntimeToolResourceKB (conditional p90 + backoff)
  - tool_resource.metrics (ecdf_quantile / pinball_loss)

Usage::

    python scripts/swe_rebench_resource_demo.py <dataset_root> [--max-tasks N]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from legacy_eval._bootstrap import ensure_paths  # noqa: E402

ensure_paths()

from legacy_eval.split import repo_prefix, split_observations_by_repo  # noqa: E402
from tool_resource.metrics import ecdf_quantile, pinball_loss  # noqa: E402
from tool_resource.runtime_kb import RuntimeToolResourceKB  # noqa: E402


def _parse_iso(ts: str) -> float:
    ts = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(ts).timestamp()


def _parse_mem(mem_usage: str) -> float:
    """Parse '7.06MiB / 0.0MiB' -> MB."""
    import re

    head = (mem_usage or "").split("/")[0].strip()
    m = re.match(r"([0-9.]+)\s*([A-Za-z]+)", head)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("g"):
        return num * 1024.0
    if unit.startswith("k"):
        return num / 1024.0
    return num  # MiB / MB


def _samples_from_resources(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    samples = []
    for s in data.get("samples", []):
        epoch = s.get("epoch")
        if epoch is None:
            continue
        samples.append(
            {
                "epoch": float(epoch),
                "mem_mb": _parse_mem(s.get("mem_usage", "")),
                "cpu_pct": _safe_float(s.get("cpu_percent")),
                "disk_read": _safe_int(s.get("disk_read_bytes")),
                "disk_write": _safe_int(s.get("disk_write_bytes")),
                "net_rx": _safe_float(s.get("net_rx_bytes")),
                "net_tx": _safe_float(s.get("net_tx_bytes")),
            }
        )
    return sorted(samples, key=lambda x: x["epoch"])


def _safe_float(v):
    import re

    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        m = re.match(r"[0-9.]+", v.strip())
        return float(m.group(0)) if m else 0.0
    return 0.0


def _safe_int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _attributed_call(rec: dict, samples: list[dict]) -> dict:
    tool = rec.get("tool", "?")
    start = _parse_iso(rec.get("timestamp", ""))
    end = _parse_iso(rec.get("end_timestamp", "")) or start
    duration_ms = rec.get("duration_ms")
    latency_ms = float(duration_ms) if isinstance(duration_ms, (int, float)) else max(0.0, (end - start) * 1000.0)
    inp = rec.get("input")
    command = inp.get("command") if isinstance(inp, dict) else None
    command = command if isinstance(command, str) else None

    in_window = [s for s in samples if start <= s["epoch"] <= end + 1e-6]
    if not in_window:
        # nearest sample for level metrics; zero deltas
        nearest = min(samples, key=lambda s: abs(s["epoch"] - start)) if samples else None
        mem = nearest["mem_mb"] if nearest else 0.0
        cpu = nearest["cpu_pct"] if nearest else 0.0
        disk_r = disk_w = net_rx = net_tx = 0.0
    else:
        mem = max(s["mem_mb"] for s in in_window)
        cpu = statistics.fmean(s["cpu_pct"] for s in in_window)
        disk_r = max(0.0, in_window[-1]["disk_read"] - in_window[0]["disk_read"]) / (1024 * 1024)
        disk_w = max(0.0, in_window[-1]["disk_write"] - in_window[0]["disk_write"]) / (1024 * 1024)
        net_rx = max(0.0, in_window[-1]["net_rx"] - in_window[0]["net_rx"]) / (1024 * 1024)
        net_tx = max(0.0, in_window[-1]["net_tx"] - in_window[0]["net_tx"]) / (1024 * 1024)

    return {
        "tool": tool,
        "command": command,
        "latency_ms": latency_ms,
        "mem_mb": mem,
        "cpu_cores": cpu / 100.0,  # cpu_percent (of one core) -> cores
        "disk_read_mb": disk_r,
        "disk_write_mb": disk_w,
        "net_rx_mb": net_rx,
        "net_tx_mb": net_tx,
    }


def load_task(task_dir: Path, repo: str) -> list[dict]:
    attempt = task_dir / "attempt_1"
    tc = attempt / "tool_calls.json"
    res = attempt / "resources.json"
    if not tc.is_file() or not res.is_file():
        return []
    calls = json.loads(tc.read_text(encoding="utf-8"))
    samples = _samples_from_resources(res)
    if not samples:
        return []
    out = []
    for rec in calls:
        if not isinstance(rec, dict):
            continue
        obs = _attributed_call(rec, samples)
        obs["repo"] = repo
        out.append(obs)
    return out


def _node_keys(tool_name: str, command: str | None) -> list[tuple[str, str]]:
    if command:
        head = command.split()[0] if command.split() else None
        keys = [("command", command)]
        if head:
            keys.append(("binary_head", head))
    else:
        keys = []
    keys.append(("tool_name", tool_name))
    keys.append(("global", ""))
    return keys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_root")
    parser.add_argument("--max-tasks", type=int, default=None)
    parser.add_argument("--train-frac", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--targets", default="mem_mb,cpu_cores,disk_rw_mb,net_rw_mb")
    args = parser.parse_args()

    root = Path(args.dataset_root)
    task_dirs = sorted(d for d in root.iterdir() if d.is_dir() and d.name.count("__") >= 1)
    if args.max_tasks:
        task_dirs = task_dirs[: args.max_tasks]

    all_obs: list[dict] = []
    skipped = 0
    for task_dir in task_dirs:
        repo = repo_prefix(task_dir.name)
        obs = load_task(task_dir, repo)
        if not obs:
            skipped += 1
        all_obs.extend(obs)
    print(f"tasks={len(task_dirs)} (skipped-no-data={skipped}) tool_calls={len(all_obs)}")

    # derived targets
    for o in all_obs:
        o["disk_rw_mb"] = o["disk_read_mb"] + o["disk_write_mb"]
        o["net_rw_mb"] = o["net_rx_mb"] + o["net_tx_mb"]
        o["key"] = (o["repo"], o["tool"], o.get("tool_call_id"))

    # per-repo observation-level split (reuse existing split); unique call id = index
    observations = [(o["repo"], o["repo"], str(i)) for i, o in enumerate(all_obs)]
    train_keys, test_keys = split_observations_by_repo(
        observations, train_frac=args.train_frac, seed=args.seed
    )
    train = [o for i, o in enumerate(all_obs) if (o["repo"], str(i)) in train_keys]
    test = [o for i, o in enumerate(all_obs) if (o["repo"], str(i)) in test_keys]
    print(f"train={len(train)} test={len(test)}")

    targets = [t.strip() for t in args.targets.split(",") if t.strip()]

    # Build one RuntimeToolResourceKB per target (reuse its public/repo/p90 logic).
    for target in targets:
        kb = RuntimeToolResourceKB()
        # public layer: per tool_name / global
        public: dict[tuple[str, str], list[float]] = defaultdict(list)
        repo_layer: dict[str, dict[tuple[str, str], list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for o in train:
            value = o[target]
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            for key in _node_keys(o["tool"], o["command"]):
                public[key].append(value)
            for key in _node_keys(o["tool"], o["command"]):
                repo_layer[o["repo"]][key].append(value)
        kb._public = {target: {k: tuple(v) for k, v in public.items()}}  # type: ignore[attr-defined]
        kb._repo = {  # type: ignore[attr-defined]
            repo: {target: {k: v for k, v in nodes.items()}}
            for repo, nodes in repo_layer.items()
        }

        records = []
        for o in test:
            actual = o[target]
            if not isinstance(actual, (int, float)) or not math.isfinite(actual):
                continue
            # repo-layer first, then public (deepest non-empty wins)
            values = None
            for key in _node_keys(o["tool"], o["command"]):
                rv = kb._repo.get(o["repo"], {}).get(target, {}).get(key)  # type: ignore[attr-defined]
                if rv:
                    values = rv
                    break
            if values is None:
                for key in _node_keys(o["tool"], o["command"]):
                    pv = kb._public[target].get(key)  # type: ignore[attr-defined]
                    if pv:
                        values = pv
                        break
            if values is None:
                continue
            predicted = ecdf_quantile(values, 0.9)
            records.append((actual, predicted))

        if not records:
            print(f"  {target:12s}: no usable records")
            continue
        actuals = [a for a, _ in records]
        preds = [p for _, p in records]
        pinball = statistics.fmean(pinball_loss(a, p, 0.9) for a, p in records)
        cov = len(records) / len(test)
        # Calibration: fraction of calls whose actual value is within the
        # predicted p90 (an ideal p90 estimator covers ~90% of cases).
        calibration = sum(1 for a, p in records if a <= p) / len(records)
        print(
            f"  {target:12s}: n={len(records)} data-cov={cov * 100:.1f}% "
            f"p90-coverage={calibration * 100:.1f}% "
            f"pinball(q=.9)={pinball:8.3f} "
            f"mean pred={statistics.fmean(preds):9.3f} / actual={statistics.fmean(actuals):9.3f} "
            f"| q.9 pred={ecdf_quantile(preds, 0.9):9.3f} / actual={ecdf_quantile(actuals, 0.9):9.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
