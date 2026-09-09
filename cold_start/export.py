from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from tool_resource.runtime_kb import ClauseResourceKB, RuntimeToolResourceKB
from tool_time.lattice_kb import LatticeTimeKB
from tool_time.resource_lattice import resource_values

from cold_start.flat_loader import read_task
from cold_start.manifest import sha256_file, validate_manifest, write_json

FILENAMES = ("clause-resource-kb.json", "runtime-tool-resource-kb.json", "clause-lattice-time-kb.json")


def export(dataset: Path, manifest: dict, output: Path, *, rss_unit: str,
           trust_call_cgroup: bool = False, progress=print) -> dict:
    dataset, output = dataset.resolve(strict=True), output.resolve()
    if output.is_relative_to(dataset):
        raise ValueError("output must be outside the read-only dataset")
    if any((output / name).exists() for name in (*FILENAMES, "seed-manifest.json")):
        raise ValueError("export requires a new output directory; publish the verified bundle explicitly")
    validate_manifest(manifest, dataset)
    clauses, calls, counts, target_counts = [], [], Counter(), Counter()
    seen_files = set()
    for index, task in enumerate(manifest["train_tasks"], 1):
        entry = manifest["tasks"][task]
        for file in entry["files"]:
            if file["sha256"] in seen_files:
                continue
            seen_files.add(file["sha256"])
            loaded = read_task(dataset / file["path"], repo=entry["repo"], task_id=task,
                               rss_unit=rss_unit, trust_call_cgroup=trust_call_cgroup)
            clauses.extend(loaded.clauses)
            calls.extend(loaded.calls)
            counts.update(loaded.counts)
            for row in loaded.clauses:
                target_counts.update(resource_values(row).keys())
        if index % 25 == 0:
            progress(f"loaded {index}/{len(manifest['train_tasks'])} training tasks; {len(clauses)} clauses, {len(calls)} calls", flush=True)
    if not clauses or not calls:
        raise ValueError("training split lacks clause or call evidence")
    trie = ClauseResourceKB.fit_public(clauses)
    runtime = RuntimeToolResourceKB.fit_public(calls)
    # Keep repository-specific training nodes: test tasks are from these same
    # repositories, not unseen repos. Finalize training before freezing.
    for row in clauses:
        trie.observe_completed_clause(row)
    for call in calls:
        runtime.observe_completed_call(call)
    trie._absorb_completed(float("inf"))
    runtime._absorb_completed(float("inf"))
    lattice = LatticeTimeKB.fit(clauses)
    payloads = (trie.to_json_obj(), runtime.to_json_obj(), lattice.to_json_obj())
    for name, payload in zip(FILENAMES, payloads):
        write_json(output / name, payload)
    hashes = {name: sha256_file(output / name) for name in FILENAMES}
    result = {"schema": "clawtune_cold_start.v1", "split_manifest_sha256": manifest["manifest_sha256"],
              "train_tasks": manifest["train_tasks"], "test_tasks": manifest["test_tasks"],
              "rss_source_unit": rss_unit, "trust_call_cgroup": trust_call_cgroup,
              "training_only": True, "repository_nodes_included": True,
              "snapshots": hashes, "counts": dict(counts), "clause_target_counts": dict(target_counts),
              "runtime_target_counts": {target: len(nodes.get(("global", ""), ())) for target, nodes in runtime._public.items()}}
    write_json(output / "seed-manifest.json", result)
    write_json(output / "split-manifest.json", manifest)
    write_json(output / "test-tasks.json", {"tasks": manifest["test_tasks"], "split_manifest_sha256": manifest["manifest_sha256"]})
    return result
