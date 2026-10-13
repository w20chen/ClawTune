"""Read-only attribution of archived task-held-out duration predictions.

Run with the matching checkout on PYTHONPATH. Output belongs outside the trace
dataset. The index stage verifies archived rows against the production loader;
the evidence stage replays selected calls against the original frozen snapshots.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "research/command_parser_evaluation"


def read(path):
    path = Path(path)
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def archived(version):
    return [json.loads(line) for line in gzip.decompress(
        (ARCHIVE / version / "model-predictions.jsonl.gz").read_bytes()).splitlines()]


def raw_actions(dataset, task):
    seen = set()
    for file in task["files"]:
        if file["sha256"] in seen:
            continue
        seen.add(file["sha256"])
        path = dataset / file["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == file["sha256"]
        with path.open(encoding="utf8") as stream:
            for number, line in enumerate(stream, 1):
                if '"tool_exec"' not in line:
                    continue
                row = json.loads(line)
                if row.get("type") == "action" and row.get("action_type") == "tool_exec":
                    yield file["path"], number, row


def raw_summary(file, line, row):
    data = row["data"]
    obs = data.get("resource_observation") or {}
    return {"file": file, "line": line, "action_id": row["action_id"],
            "tool_call_id": data.get("tool_call_id"), "hook_duration_ms": data.get("duration_ms"),
            "success": data.get("success"), "exit_code": data.get("exit_code"),
            "data_fields": list(data),
            "output_excerpt": str(data.get("tool_result", data.get("result", data.get("output", ""))))[:3000],
            "clauses": [{k: c.get(k) for k in ("argv", "latency_ms", "ts_start", "ts_end", "eligible_for_kb", "telemetry_quality", "in_loop", "in_subst", "in_pipe", "pipeline_position", "stdin_from_pipe")}
                        for c in obs.get("clauses", [])]}


def index(dataset, output):
    from offline.runner import _call_actuals, load_task
    split = read(ARCHIVE / "main/split.json.gz")
    by_version = {v: defaultdict(list) for v in ("phase1", "main")}
    for v in by_version:
        for row in archived(v):
            if row["target"] == "duration_ms":
                by_version[v][(row["task"], row["model"])].append(row)
    calls = []
    for t, key in enumerate(split["test"]):
        task = split["tasks"][key]
        loaded = load_task(dataset, task, "MiB")
        raw = [(f, n, r) for f, n, r in raw_actions(dataset, task)
               if isinstance(r["data"].get("duration_ms"), (float, int))
               and math.isfinite(r["data"]["duration_ms"]) and r["data"]["duration_ms"] >= 0]
        assert len(raw) == len(loaded.calls), (key, len(raw), len(loaded.calls))
        scored = 0
        for i, call in enumerate(loaded.calls):
            if call.censored:
                continue
            actuals = {**_call_actuals(call), **loaded.call_actuals[i]}
            if "duration_ms" not in actuals:
                continue
            rec = {"id": key + ":" + str(i), "task": key, "call_index": i,
                   "repo": call.repo, "tool": call.tool_name, "command": call.command,
                   "actual_ms": actuals["duration_ms"], "raw": raw_summary(*raw[i]), "models": {}}
            for model in ("tool", "trie", "lattice", "edge_kappa"):
                rec["models"][model] = {}
                for v in by_version:
                    row = by_version[v][(key, model)][scored]
                    assert math.isclose(row["actual"], actuals["duration_ms"], rel_tol=1e-12), (key, i, row)
                    assert row["tool"] == call.tool_name
                    rec["models"][model][v] = row
            calls.append(rec)
            scored += 1
        for v in by_version:
            for model in rec["models"] if scored else ("tool", "trie", "lattice", "edge_kappa"):
                assert len(by_version[v][(key, model)]) == scored
        if t % 20 == 0:
            print("indexed", t, "tasks", len(calls), "calls", flush=True)
    def ae(c, model, v="main"):
        p = c["models"][model][v]["p50"]
        return abs(p - c["actual_ms"]) if p is not None else -1
    ranks = {m: [c["id"] for c in sorted(calls, key=lambda c: ae(c, m), reverse=True)[:30]]
             for m in ("tool", "trie", "lattice", "edge_kappa")}
    ranks["unique_lattice_edge"] = [c["id"] for c in sorted(calls,
        key=lambda c: max(ae(c, "lattice"), ae(c, "edge_kappa")), reverse=True)[:30]]
    selected = {key for group in ranks.values() for key in group}
    transitions = {}
    for model in ("lattice", "edge_kappa"):
        changed = []
        for c in calls:
            a, b = (c["models"][model][v] for v in ("phase1", "main"))
            if a["p50"] is not None and b["p50"] is not None and a["predicted_bucket"] != b["predicted_bucket"]:
                changed.append(c["id"])
        transitions[model] = changed
        selected.update(changed)
        largest = sorted((c for c in calls if c["models"][model]["phase1"]["p50"] is not None
                          and c["models"][model]["main"]["p50"] is not None),
                         key=lambda c: ae(c, model) - ae(c, model, "phase1"), reverse=True)
        ranks[model + "_regression"] = [c["id"] for c in largest[:30]]
        selected.update(ranks[model + "_regression"])
    write(output / "index.json", {"calls": calls, "rankings": ranks, "bucket_transitions": transitions,
                                  "selected": sorted(selected), "split_sha256": split["assignment_sha256"]})
    by_id = {c["id"]: c for c in calls}
    raw_top = []
    for rank, key in enumerate(ranks["unique_lattice_edge"], 1):
        c = by_id[key]
        with (dataset / c["raw"]["file"]).open(encoding="utf8") as stream:
            row = next(json.loads(line) for number, line in enumerate(stream, 1)
                       if number == c["raw"]["line"])
        text = str(row["data"].get("tool_result", ""))
        raw_top.append({"rank": rank, "id": key, "command": c["command"],
            "output_tail": text[-4500:], "output_length": len(text), "clauses": c["raw"]["clauses"],
            "exit_code_agreement": row["data"].get("exit_code_agreement")})
    write(output / "raw-top30.json", raw_top)
    print("indexed", len(calls), "selected", len(selected), flush=True)
    for id_ in ranks["unique_lattice_edge"]:
        c = next(c for c in calls if c["id"] == id_)
        print(c["id"], round(c["actual_ms"] / 1000, 3),
              [round(c["models"][m]["main"]["p50"] / 1000, 3) if c["models"][m]["main"]["p50"] is not None else None
               for m in ("lattice", "edge_kappa")], repr(c["command"][:240] if c["command"] else c["tool"]), flush=True)


class Capture:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.clauses = []
        self.rows = []

    def predict_load_samples(self, repo, clauses, ts_start, **kwargs):
        self.clauses = list(clauses)
        self.rows = self.wrapped.predict_load_samples(repo, clauses, ts_start, **kwargs)
        return self.rows


class EmptyRuntime:
    def predict_load_samples(self, query):
        return {}


def evidence(version, snapshot, output):
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
    from tool_resource.runtime_kb import (
        ClauseResourceKB,
        ToolCallQuery,
        is_pipeline_dependent_consumer,
    )
    from tool_time._lattice_vendor.normalize import normalize_command
    from tool_time.lattice_kb import LatticeTimeKB

    from offline.runner import _safe_recorded_clause
    try:
        from command_parser.service import clause_features, observation_features
        from tool_time.edge_kappa_adapter import feature_query
    except ImportError:
        observation_features = clause_features = None
        from tool_time.edge_kappa_adapter import shell_query
    audit = read(output / "index.json")
    lattice = LatticeTimeKB.from_json_obj(read(snapshot / "clause-lattice-time-kb.json"))
    lattice.freeze()
    edge = EdgeKappaRuntime.from_snapshot(read(snapshot / "edge-kappa-kb.json"), frozen=True)
    print("loaded", version, "snapshots", flush=True)
    train = []
    for obs in lattice._observations:
        if obs.latency_ms is None or obs.in_loop or obs.in_subst or is_pipeline_dependent_consumer(obs):
            continue
        if observation_features:
            f = observation_features(obs)
            features, core = f.features, f.core
        else:
            import shlex
            features, core = normalize_command(shlex.join(obs.argv), repo=obs.repo)
        train.append((obs, features, core))
    evidence_cache = {}

    def node_sources(features, durations):
        key = tuple(sorted(features))
        if key not in evidence_cache:
            matched = [(o, f) for o, f, core in train if set(features).issubset(f)]
            grouped = defaultdict(list)
            for o, f in matched:
                grouped[(o.repo, tuple(o.argv))].append(o.latency_ms)
            # Full command groups are retained for an auditable sample attribution.
            groups = [{"repo": repo, "argv": list(argv), "n": len(vals),
                       "durations_ms": sorted(vals)} for (repo, argv), vals in grouped.items()]
            groups.sort(key=lambda g: (-g["n"], g["repo"], str(g["argv"])))
            values = [o.latency_ms for o, _ in matched]
            expected = Counter(round(v, 6) for v in durations)
            found = Counter(round(v, 6) for v in values)
            evidence_cache[key] = {"features": list(key), "matched_observations": len(matched),
                "duration_multiset_matches": expected == found, "groups": groups}
        return key

    selected = [c for c in audit["calls"] if c["id"] in audit["selected"]]
    result = []
    for i, c in enumerate(selected):
        lc, ec = Capture(lattice), Capture(edge)
        recorded = tuple({k: raw.get(k) for k in ("argv", "in_loop", "in_subst", "in_pipe", "pipeline_position", "stdin_from_pipe")}
                         | {"bin": raw["argv"][0].rsplit("/", 1)[-1]} for raw in c["raw"]["clauses"] if raw.get("argv"))
        known = _safe_recorded_clause(c["command"], recorded)
        _, diag = predict_call_load(runtime=EmptyRuntime(), trie=ClauseResourceKB(), lattice=lc,
            edge_kappa=ec, query=ToolCallQuery(c["repo"], c["tool"], c["command"], 1e12),
            edges=load_bucket_edges((100, 500, 2000, 10000)), parsed_clauses=known)
        item = {"id": c["id"], "models": {}, "clauses": []}
        for model in ("lattice", "edge_kappa"):
            estimate = diag.backends[model].targets["duration_ms"]
            expected = c["models"][model][version]
            assert (estimate.p50 is None and expected["p50"] is None) or (
                estimate.p50 is not None and expected["p50"] is not None
                and math.isclose(estimate.p50, expected["p50"], rel_tol=1e-10, abs_tol=1e-6)), (version, c["id"], model, estimate.p50, expected["p50"])
            item["models"][model] = estimate.model_dump()
        retained = [cl for cl in lc.clauses if not is_pipeline_dependent_consumer(cl)]
        for j, cl in enumerate(retained):
            detail = {"argv": cl["argv"], "syntax_argv": cl.get("syntax_argv"),
                      "in_pipe": cl.get("in_pipe"), "prediction_unavailable_reason": cl.get("prediction_unavailable_reason")}
            if clause_features:
                record = clause_features(cl, c["repo"])
                detail["features"] = record.to_dict()
                from command_parser import ParseContext, default_service
                detail["parsed"] = default_service().parser.parse(cl.get("syntax_argv", cl["argv"]), ParseContext(repo=c["repo"])).to_dict()
                query = feature_query(record)
            else:
                import shlex
                f, core = normalize_command(shlex.join(cl["argv"]), repo=c["repo"])
                detail["features"] = {"features": sorted(f), "core": sorted(core)}
                query = shell_query(shlex.join(cl["argv"]), repo=c["repo"])
            if j < len(lc.rows) and lc.rows[j].get("duration_ms", {}).get("values"):
                row = lc.rows[j]["duration_ms"]
                fs = frozenset(row["context"][1:])
                key = node_sources(fs, row["values"])
                detail["lattice"] = {"selected_features": list(key), "n": len(row["values"]),
                    "exact": set(key) == set(detail["features"]["features"]),
                    "p50": statistics.median(row["values"]), "min": min(row["values"]), "max": max(row["values"]),
                    "bucket_counts": list(Counter(__import__('bisect').bisect_right((100, 500, 2000, 10000), v) for v in row["values"]).items())}
            if not cl.get("prediction_unavailable_reason"):
                dist = edge.kb.predict_duration(query)
                detail["edge"] = asdict(dist.bucket_prediction)
                # Parent features and differences are present in BucketPrediction.
            item["clauses"].append(detail)
        result.append(item)
        if i % 20 == 0:
            print(version, "replayed", i, "/", len(selected), flush=True)
    write(output / (version + "-evidence.json"), {"calls": result,
        "nodes": list(evidence_cache.values()), "training_observations": len(train), "version": version})
    print(version, "verified", len(result), "calls", len(evidence_cache), "nodes", flush=True)


def probes(snapshot, output):
    """Diagnostic counterfactuals on selected queries, not a validation split."""
    from command_parser.models import CommandFeatures
    from command_parser.service import observation_features
    from tool_resource.runtime_kb import is_pipeline_dependent_consumer
    from tool_time._lattice_vendor.risk import compute_candidate_risk
    from tool_time.lattice_kb import _CONTEXT_SAMPLE_ALPHA, LatticeTimeKB
    from tool_time.resource_lattice import ResourceState

    kb = LatticeTimeKB.from_json_obj(read(snapshot / "clause-lattice-time-kb.json"))
    kb.freeze()
    kb._ensure_nodes()
    state = kb._resource_states["load:duration_ms"]
    training = [(o, observation_features(o)) for o in kb._observations
                if o.latency_ms is not None and not o.in_loop and not o.in_subst
                and not is_pipeline_dependent_consumer(o)]
    ev = read(output / "main-evidence.json")
    idx = read(output / "index.json")
    calls = {c["id"]: c for c in idx["calls"]}
    cache, result = {}, []
    for call in ev["calls"]:
        for index, cl in enumerate(call["clauses"]):
            if not cl.get("lattice"):
                continue
            record = CommandFeatures.from_dict(cl["features"])
            key = tuple(sorted(record.features))
            if key not in cache:
                candidates = []
                for fs, stats in state.nodes.items():
                    if fs <= record.features:
                        risk = compute_candidate_risk(stats, record.features,
                            global_log_var=state.global_log_var, global_log_std=state.global_log_std,
                            risk_method="shrinkage",
                            context_sample_alpha=_CONTEXT_SAMPLE_ALPHA, gamma=0)
                        candidates.append({"features": sorted(fs), "n": stats.count,
                                           "p50_ms": statistics.median(stats.durations) * 1000,
                                           "risk": risk.risk, "keeps_core": record.core <= fs})
                restricted = ResourceState({fs: st for fs, st in state.nodes.items() if record.core <= fs},
                                           state.global_log_var, state.global_log_std)
                alternative = restricted.predict(calls[call["id"]]["repo"], cl["argv"], "duration_ms",
                                                 "shrinkage", command_features=record).to_dict()
                exact = [o.latency_ms for o, f in training if f.features == record.features]
                cache[key] = {"query_features": list(key), "core": sorted(record.core),
                    "original_selected": cl["lattice"], "core_required_at_query": alternative,
                    "same_signature_only": {"n": len(exact), "p50_ms": statistics.median(exact) if exact else None},
                    "candidates": sorted(candidates, key=lambda r: (r["risk"], -r["n"]))}
            result.append({"id": call["id"], "clause_index": index, "probe": cache[key]})
    write(output / "probes.json", {"scope": "diagnostic selected cases; no claim of held-out improvement",
                                   "clauses": result})
    print("probed", len(result), "clauses", len(cache), "unique queries", flush=True)


def sources(dataset, output):
    split = read(ARCHIVE / "main/split.json.gz")
    wanted = set()
    for version in ("main", "phase1"):
        for node in read(output / (version + "-evidence.json"))["nodes"]:
            for group in node["groups"]:
                for value in group["durations_ms"]:
                    wanted.add((group["repo"], tuple(group["argv"]), round(value, 6)))
    found = defaultdict(list)
    files_verified = set()
    for i, key in enumerate(split["train"]):
        task = split["tasks"][key]
        repo = task["benchmark"] + ":" + task["group"]
        for file, line, row in raw_actions(dataset, task):
            files_verified.add(file)
            for index, cl in enumerate((row["data"].get("resource_observation") or {}).get("clauses", [])):
                if not isinstance(cl.get("latency_ms"), (int, float)) or not cl.get("argv"):
                    continue
                signature = (repo, tuple(cl["argv"]), round(cl["latency_ms"], 6))
                if signature in wanted:
                    found[signature].append({"task": key, "file": file, "line": line,
                        "action_id": row["action_id"], "clause_index": index})
        if i % 100 == 0:
            print("training sources", i, "matched signatures", len(found), flush=True)
    write(output / "training-sources.json", {"train_tasks_verified": len(split["train"]),
        "wanted": len(wanted), "matched": len(found),
        "missing": [[repo, list(argv), value] for repo, argv, value in sorted(wanted - found.keys())],
        "sources": [{"repo": repo, "argv": list(argv), "duration_ms": value, "references": refs}
                    for (repo, argv, value), refs in sorted(found.items())]})
    print("sources verified", len(found), "/", len(wanted), flush=True)


def controls(snapshot, output):
    from clawtune_sidecar.prediction_config import load_bucket_edges
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from tool_resource.runtime_kb import (
        ClauseResourceKB,
        RuntimeToolResourceKB,
        ToolCallQuery,
    )
    from tool_time.lattice_kb import LatticeTimeKB

    from offline.runner import _safe_recorded_clause

    runtime = RuntimeToolResourceKB.from_json_obj(read(snapshot / "runtime-tool-resource-kb.json"))
    trie = ClauseResourceKB.from_json_obj(read(snapshot / "clause-resource-kb.json"))
    runtime.freeze()
    trie.freeze()
    audit = read(output / "index.json")
    results = []
    for c in audit["calls"]:
        if c["id"] not in audit["selected"]:
            continue
        query = ToolCallQuery(c["repo"], c["tool"], c["command"], 1e12)
        captured = Capture(trie)
        recorded = tuple({k: raw.get(k) for k in ("argv", "in_loop", "in_subst", "in_pipe", "pipeline_position", "stdin_from_pipe")}
                         | {"bin": raw["argv"][0].rsplit("/", 1)[-1]} for raw in c["raw"]["clauses"] if raw.get("argv"))
        prediction, diag = predict_call_load(runtime=runtime, trie=captured, lattice=LatticeTimeKB(), query=query,
            edges=load_bucket_edges((100, 500, 2000, 10000)),
            parsed_clauses=_safe_recorded_clause(c["command"], recorded))
        item = {"id": c["id"], "tool": prediction.targets["duration_ms"].model_dump(),
                "trie": diag.backends["trie"].targets["duration_ms"].model_dump(), "trie_clauses": []}
        for model in ("tool", "trie"):
            expected = c["models"][model]["main"]["p50"]
            got = item[model]["p50"]
            assert (got is None and expected is None) or (
                got is not None and expected is not None and math.isclose(got, expected, rel_tol=1e-10, abs_tol=1e-6))
        for clause in diag.backends["trie"].clause_predictions:
            item["trie_clauses"].append({"argv": clause.argv, "estimate": clause.targets["duration_ms"].model_dump()})
        results.append(item)
    write(output / "controls.json", results)
    print("verified controls", len(results), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=("index", "evidence", "probes", "sources", "controls"))
    p.add_argument("--dataset", type=Path)
    p.add_argument("--version", choices=("main", "phase1"), default="main")
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.dataset and (args.output.resolve().is_relative_to(args.dataset.resolve())
                         or args.dataset.resolve().is_relative_to(args.output.resolve())):
        p.error("analysis output and read-only dataset must be separate directories")
    if args.stage == "index":
        index(args.dataset, args.output)
    elif args.stage == "evidence":
        evidence(args.version, args.snapshot, args.output)
    elif args.stage == "probes":
        probes(args.snapshot, args.output)
    elif args.stage == "sources":
        sources(args.dataset, args.output)
    else:
        controls(args.snapshot, args.output)
