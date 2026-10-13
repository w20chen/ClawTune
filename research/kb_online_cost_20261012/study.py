"""Current-code KB timing on an immutable corpus; all writes stay in this study.

Run each case in a separate process. Timings include actual algorithms, not an
alternative implementation. Progress wrappers only record phase boundaries.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import statistics
import sys
import threading
import time

import psutil

from clawtune_sidecar.predictors.call_load import compose
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime, training_event
from clawtune_sidecar.prediction_config import load_bucket_edges
from command_parser.service import observation_features
from edge_kappa_kb import EdgeKappaKB
from edge_kappa_kb.graph import FeatureGraph
from tool_resource.runtime_kb import ClauseObservation
from tool_time.lattice_kb import LatticeTimeKB


def stats(values):
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(values), "mean_ms": statistics.mean(values),
            "p50_ms": statistics.median(values),
            "p95_ms": ordered[math.ceil(.95 * len(values)) - 1],
            "p99_ms": ordered[math.ceil(.99 * len(values)) - 1],
            "max_ms": ordered[-1], "total_ms": sum(values)}


def timed(fn, *args, **kwargs):
    start = time.perf_counter_ns()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter_ns() - start) / 1e6


def observation(data, index=None):
    args = dict(data, argv=tuple(data["argv"]))
    if index is not None:
        args.update(ts_start=3e12 + index * 1000, ts_end=3e12 + index * 1000 + 1)
    return ClauseObservation(**args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("model", choices=("lattice", "edge", "edge-cold", "pair"))
    parser.add_argument("limit", type=int)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--training-count", type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    name = f"{args.model}-{args.limit}" + (f"-n{args.training_count}" if args.training_count else "")
    result_path = args.out / (name + ".json")
    journal = (args.out / (name + "-rows.jsonl")).open("w")
    start = time.perf_counter()
    phase = {"stage": "load"}
    result = {"model": args.model, "limit": args.limit,
              "python": sys.version, "platform": platform.platform(),
              "gil_enabled": getattr(sys, "_is_gil_enabled", lambda: True)(),
              "cpu_affinity": sorted(os.sched_getaffinity(0)),
              "host_cpu_count": os.cpu_count(), "load_before": os.getloadavg(),
              "memory_available_before": psutil.virtual_memory().available,
              "scope": "prepared clauses; actual KB algorithms and composition; excludes HTTP and raw shell parsing"}
    save_lock = threading.Lock()
    def save():
        with save_lock:
            result.update(phase=dict(phase), elapsed_s=time.perf_counter() - start,
                          peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
                          process_cpu_s=time.process_time())
            temp = result_path.with_suffix(".tmp")
            temp.write_text(json.dumps(result, indent=2))
            temp.replace(result_path)
    def mark(stage, **kwargs):
        phase.clear()
        phase.update(stage=stage, **kwargs)
        save()
        print(name, stage, round(time.perf_counter()-start, 3), flush=True)
    done = threading.Event()
    def heartbeat():
        while not done.wait(10):
            save()
    threading.Thread(target=heartbeat, daemon=True).start()
    def record(kind, **kwargs):
        journal.write(json.dumps({"kind": kind, **kwargs}) + "\n")
        journal.flush()

    corpus_bytes = (args.root / "training.json").read_bytes()
    inputs_bytes = (args.root / "inputs.json").read_bytes()
    result.update(corpus_sha256=hashlib.sha256(corpus_bytes).hexdigest(),
                  inputs_sha256=hashlib.sha256(inputs_bytes).hexdigest())
    raw = json.loads(corpus_bytes)["observations"]
    if args.training_count:
        # Deterministic spread across the sorted corpus, not its first repository.
        raw = [raw[i * len(raw) // args.training_count] for i in range(args.training_count)]
    rows = [observation(row) for row in raw]
    inputs = json.loads(inputs_bytes)
    samples = inputs["samples"]
    updates = [observation(row, i) for i, row in enumerate(inputs["update_clauses"])]
    result.update(training_rows=len(rows), test_calls=len(samples),
                  optional_feature_histogram=dict(collections.Counter(
                      len((f := observation_features(row)).features - f.core) for row in rows)))
    edges = load_bucket_edges((100, 500, 2000, 10000))

    if args.model == "pair":
        pair(args, rows, samples, updates, edges, result, mark, record)
        mark("done")
        done.set()
        return

    mark("fit")
    build_start = time.perf_counter()
    if args.model == "lattice":
        kb, result["ingest_ms"] = timed(LatticeTimeKB.fit, rows, max_optional_features=args.limit)
        mark("prepare")
        _, result["prepare_ms"] = timed(kb.prepare)
        result["duration_nodes"] = len(kb._resource_states.get("load:duration_ms").nodes)
        result["view_node_counts"] = {k: len(v.nodes) for k, v in kb._resource_states.items()}
    elif args.model == "edge-cold":
        kb = EdgeKappaRuntime.fit([], edges["duration_ms"], max_optional_features=args.limit)
        # The actual empty-bootstrap online path only adds full nodes.
        cold_begin, cold_complete = [], []
        mark("cold_online_history")
        for i, row in enumerate(rows):
            if training_event(row) is None:
                continue
            item = dataclasses.replace(row, ts_start=1e12 + i * 1000, ts_end=1e12 + i * 1000 + 1)
            _, ms = timed(kb.predict_load_samples, item.repo, [dataclasses.asdict(item)], item.ts_start,
                          call_id=f"cold-{i}")
            cold_begin.append(ms)
            _, ms = timed(kb.complete_call, f"cold-{i}", [item], commit_time=item.ts_end + 1)
            cold_complete.append(ms)
            phase["completed"] = i + 1
            if i % 100 == 0:
                record("cold_progress", i=i, begin=stats(cold_begin[-100:]), complete=stats(cold_complete[-100:]),
                       nodes=len(kb.kb.nodes))
        result["cold_begin"] = stats(cold_begin)
        result["cold_complete"] = stats(cold_complete)
    else:
        original_parents = FeatureGraph.canonical_parents
        original_maximal = FeatureGraph._maximal
        original_begin = EdgeKappaKB.begin
        def parents(graph):
            mark("graph_parents", total_nodes=len(graph.signatures), completed_nodes=0)
            return original_parents(graph)
        def maximal(graph, candidates):
            value = original_maximal(graph, candidates)
            if phase["stage"] == "graph_parents":
                phase["completed_nodes"] += 1
            return value
        def begin(model, *a, **kw):
            if phase["stage"] != "training_replay":
                mark("training_replay", completed_events=0, graph_nodes=len(model.nodes))
            value = original_begin(model, *a, **kw)
            phase["completed_events"] += 1
            return value
        FeatureGraph.canonical_parents, FeatureGraph._maximal, EdgeKappaKB.begin = parents, maximal, begin
        try:
            kb = EdgeKappaRuntime.fit(rows, edges["duration_ms"], max_optional_features=args.limit)
        finally:
            FeatureGraph.canonical_parents, FeatureGraph._maximal, EdgeKappaKB.begin = original_parents, original_maximal, original_begin
    result["ready_s"] = time.perf_counter() - build_start
    result["ready_rss_mib"] = psutil.Process().memory_info().rss / 2**20
    if args.model.startswith("edge"):
        result.update(graph_nodes=len(kb.kb.nodes), graph_edges=len(kb.kb.edges),
                      learned_edge_count=sum(e.update_count > 0 for e in kb.kb.edges.values()))
        # Keep a real trained state for the paired online-path measurement.
        mark("save_trained_state")
        snapshot = kb.to_snapshot()
        cache = args.out / f"{name}-trained.json"
        cache.write_text(json.dumps(snapshot, separators=(",", ":")))
        del snapshot

    mark("query")
    query_times, compose_times, combined_times = [], [], []
    status = collections.Counter()
    backend = "lattice" if args.model == "lattice" else "edge_kappa"
    for repeat in range(3):
        for i, row in enumerate(samples):
            phase.update(repeat=repeat, sample=i)
            if row["reason"]:
                continue
            evidence, query_ms = timed(kb.predict_load_samples, row["repo"], row["clauses"], 2e12 + repeat * 1000 + i)
            prediction, compose_ms = timed(compose, backend, evidence, edges, clauses=row["clauses"])
            state = prediction.targets["duration_ms"].status
            status[state] += 1
            query_times.append(query_ms)
            compose_times.append(compose_ms)
            combined_times.append(query_ms + compose_ms)
            record("query", repeat=repeat, sample=i, match_ms=query_ms, compose_ms=compose_ms,
                   status=state, p50=prediction.targets["duration_ms"].p50)
    result["read_only_query"] = stats(query_times)
    result["composition"] = stats(compose_times)
    result["read_only_combined"] = stats(combined_times)
    result["duration_status"] = dict(status)

    if args.model == "lattice":
        result["updates"] = {}
        for count in (1, 32):
            mark("update_prepare", batch_size=count)
            candidate, fork_ms = timed(kb.fork_for_update)
            enqueue = []
            for row in updates[:count]:
                _, ms = timed(candidate.observe_completed_clause, row)
                enqueue.append(ms)
            _, rebuild_ms = timed(candidate.prepare)
            _, publish_ms = timed(candidate._advance, 4e12)
            result["updates"][str(count)] = dict(fork_ms=fork_ms, enqueue=stats(enqueue),
                                                prepare_ms=rebuild_ms, activate_ms=publish_ms)
            del candidate
    else:
        mark("online_begin_complete")
        begins, completes, compositions = [], [], []
        for i, row in enumerate(updates):
            data = dataclasses.asdict(row)
            query = observation_features(row)
            existed = query.features in kb.kb.graph.by_features
            evidence, begin_ms = timed(kb.predict_load_samples, row.repo, [data], row.ts_start,
                                      call_id=f"online-{i}")
            _, compose_ms = timed(compose, "edge_kappa", evidence, edges, clauses=[data])
            _, complete_ms = timed(kb.complete_call, f"online-{i}", [row], commit_time=row.ts_end + 1)
            begins.append(begin_ms); completes.append(complete_ms); compositions.append(compose_ms)
            record("online", i=i, exact_node_existed=existed, begin_ms=begin_ms, compose_ms=compose_ms,
                   complete_ms=complete_ms, covered_nodes=len(kb.kb.graph.subsets(query.features)))
        result["online_begin"] = stats(begins)
        result["online_begin_plus_compose"] = stats([a+b for a,b in zip(begins, compositions)])
        result["online_complete"] = stats(completes)

    mark("snapshot")
    payload, result["snapshot_capture_ms"] = timed(kb.to_json_obj if args.model == "lattice" else kb.to_snapshot)
    encoded, result["snapshot_json_ms"] = timed(json.dumps, payload, separators=(",", ":"))
    result["snapshot_bytes"] = len(encoded.encode())
    def write():
        with (args.out / f"{name}-snapshot.json").open("w") as f:
            f.write(encoded); f.flush(); os.fsync(f.fileno())
    _, result["snapshot_write_fsync_ms"] = timed(write)
    mark("done")
    done.set()


def pair(args, rows, samples, updates, edges, result, mark, record):
    """Real single-writer and shared-lock behavior, with prepared-clause queries."""
    from clawtune_sidecar.predictors.tool_resource import ToolResourcePredictor
    from clawtune_sidecar.predictors.call_load import predict_call_load
    from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB, LatencyBuckets, ToolCallQuery

    mark("pair_restore")
    edge = EdgeKappaRuntime.from_snapshot(json.loads((args.out / f"edge-{args.limit}-trained.json").read_bytes()))
    lattice = LatticeTimeKB.fit(rows, max_optional_features=args.limit)
    lattice.prepare()
    destination = args.out / f"pair-{args.limit}-state"
    destination.mkdir(exist_ok=True)
    predictor = ToolResourcePredictor(
        kb=ClauseResourceKB.from_json_obj(json.loads((args.root / "trie.json").read_bytes())),
        lattice_kb=lattice, edge_kappa=edge, buckets=LatencyBuckets(edges["duration_ms"]),
        report=None, repo="study", artifact_dir=destination,
        lattice_kb_snapshot_path=destination / "lattice.json")
    predictor.continuous_kb = RuntimeToolResourceKB.from_json_obj(json.loads((args.root / "tool.json").read_bytes()))
    trace = []
    original_capture = predictor._capture_kb_snapshot_locked
    def capture(*a, **kw):
        _, ms = timed(original_capture, *a, **kw)
        trace.append({"kind": "locked_snapshot_capture", "model": kw["name"], "ms": ms})
    predictor._capture_kb_snapshot_locked = capture

    def query(row, sequence):
        t = time.perf_counter()
        with predictor._kb_lock:
            acquired = time.perf_counter()
            # Use the same path as the online owner, except raw shell parsing
            # and the compatibility per-clause diagnostic assembly.
            call, diagnostics = predict_call_load(
                runtime=predictor.continuous_kb, trie=predictor.kb, lattice=predictor.lattice_kb,
                edge_kappa=predictor.edge_kappa, query=ToolCallQuery(row["repo"], row["tool"], row["command"], time.time()),
                edges=edges, edge_call_id=f"paired-{sequence}", parsed_clauses=row["clauses"])
            predictor._edge_kappa_version += 1
            predictor._kb_writes.enqueue()
        elapsed = (time.perf_counter() - t) * 1000
        record("paired_query", sequence=sequence, elapsed_ms=elapsed, lock_wait_ms=(acquired-t)*1000,
               statuses={name: pred.targets["duration_ms"].status for name, pred in diagnostics.backends.items()})
        # These are prediction probes, not executed commands. Release their
        # unexecuted tokens via the normal empty-completion path; invent no labels.
        with predictor._kb_lock:
            predictor.edge_kappa.complete_call(f"paired-{sequence}", [], commit_time=time.time())
        return elapsed, (acquired-t)*1000

    mark("pair_baseline")
    baseline = [query(row, i) for i, row in enumerate(samples) if not row["reason"]]
    predictor.flush_kb_updates(timeout_seconds=300)
    result["pair_baseline"] = stats([x[0] for x in baseline])
    result["pair_baseline_lock_wait"] = stats([x[1] for x in baseline])
    mark("pair_background_update")
    # Completed samples use past wall-clock times to be eligible on next query.
    current = time.time()
    completed = tuple(dataclasses.replace(row, ts_start=current-3, ts_end=current-2) for row in updates)
    t = time.perf_counter()
    predictor._kb_writes.enqueue(completed)
    observations_before = lattice.observation_count
    during, waits = [], []
    i = 0
    while predictor.kb_updates_pending() and time.perf_counter()-t < 300:
        row = samples[i % len(samples)]
        if not row["reason"]:
            elapsed, waiting = query(row, 10000 + i)
            during.append(elapsed); waits.append(waiting)
        i += 1
        # Arrival every ~50ms; no busy-loop flood of snapshot-only writes.
        time.sleep(.05)
        if predictor.lattice_kb is not lattice:
            break
    result["update_visible_s"] = time.perf_counter()-t
    predictor.flush_kb_updates(timeout_seconds=300)
    result["update_durable_s"] = time.perf_counter()-t
    result["pair_during_update"] = stats(during)
    result["pair_during_update_lock_wait"] = stats(waits)
    result["pair_snapshot_capture"] = trace
    # Query after publication actually activates the completed pending rows.
    query(samples[0], 999999)
    result["published_added_observations"] = predictor.lattice_kb.observation_count - observations_before
    predictor.close()


if __name__ == "__main__":
    main()
