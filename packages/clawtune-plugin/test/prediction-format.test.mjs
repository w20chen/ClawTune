import assert from "node:assert/strict";
import test from "node:test";
import {readFileSync} from "node:fs";
import {formatCallLoadPrediction, formatTimeBuckets} from "../dist/prediction-format.js";

const example = JSON.parse(readFileSync(new URL("../../../contracts/examples/call-load.json", import.meta.url), "utf8"));

test("compact buckets retain all baselines, boundaries, ties and clause scope without diagnostics", () => {
  const call = structuredClone(example);
  Object.assign(call.targets.duration_ms, {status: "available",
    buckets: {edges: [100, 500, 2000, 10000], probabilities: [0, 0.5, 0.5, 0, 0]}});
  const prediction = {tool: {load: call}, lattice: {load: {
    ...structuredClone(example), clause_predictions: [
      {clause_index: 0, targets: {duration_ms: call.targets.duration_ms}},
    ],
  }}};
  const before = JSON.stringify(prediction);
  const text = formatTimeBuckets(prediction, "call-1").join("\n");
  assert.match(text, /tool \(mode\).*call.*#1 \[100,500ms\) \/ #2 \[500ms,2s\)/);
  assert.match(text, /trie \(mode\).*unavailable/);
  assert.match(text, /lattice \(mode\).*clause 0.*#1/);
  assert.doesNotMatch(text, /CPU|RSS|PMU|histogram|evidence|reason/);
  assert.equal(JSON.stringify(prediction), before);
});

test("compact display never invents a bucket from missing or incompatible evidence", () => {
  const call = structuredClone(example);
  call.targets.duration_ms.buckets = {edges: [1], probabilities: [0, 1]};
  const text = formatTimeBuckets({tool: {load: call}}, "call\n2").join("\n");
  assert.match(text, /Time buckets \| call 2/);
  assert.equal(text.split("unavailable").length - 1, 3);
});

test("prints independent results from all three backends with complete histogram/evidence details", () => {
  const prediction = {};
  for (const backend of ["runtime", "trie", "lattice"]) {
    const candidate = structuredClone(example);
    for (const estimate of Object.values(candidate.targets)) estimate.backend = backend;
    prediction[backend === "runtime" ? "tool" : backend] = {load: candidate};
  }
  const text = formatCallLoadPrediction(prediction).join("\n");
  for (const title of ["ToolKB", "TrieKB", "LatticeKB"]) assert.ok(text.includes(title));
  for (const label of ["Duration", "CPU time", "CPU average", "CPU peak", "Memory total", "Memory extra"]) {
    assert.equal(text.split("\n").filter(line => line.startsWith(`    ${label.padEnd(12)} `)).length, 3);
  }
  assert.ok(text.includes("Mean") && text.includes("P50") && text.includes("P90"));
  assert.ok(text.includes("historical=[") && text.includes("summary samples="));
  assert.ok(text.includes("histogram (MiB") && text.includes("inf):"));
  assert.ok(text.includes("CPU peak window=500 ms"));
});

test("unavailable values, assumptions and absent diagnostics stay explicit", () => {
  const candidate = structuredClone(example);
  const estimate = candidate.targets.cpu_peak_cores;
  Object.assign(estimate, {status: "unavailable", method: "unavailable", avg: null, p50: null,
    p90: null, sample_count: 0, evidence_counts: [], unavailable_reason: "no compatible evidence"});
  estimate.buckets.probabilities = null;
  candidate.targets.duration_ms.assumptions = ["independent clause durations"];
  const text = formatCallLoadPrediction({tool: {load: candidate}}).join("\n");
  assert.match(text, /CPU peak\s+unavailable/);
  assert.ok(text.includes("reason: no compatible evidence"));
  assert.ok(text.includes("histogram: unavailable; edges (cores)="));
  assert.ok(text.includes("assumptions: independent clause durations"));
  assert.equal(text.split("- prediction not supplied").length - 1, 2);
  assert.equal(formatCallLoadPrediction({}).filter(line => line.endsWith("- prediction not supplied")).length, 3);
});

test("prints all three PMU targets with evidence and unavailable reasons", () => {
  const pmu = JSON.parse(readFileSync(new URL("../../../contracts/examples/pmu-prediction.json", import.meta.url), "utf8"));
  const text = formatCallLoadPrediction({tool: {pmu}}).join("\n");
  for (const label of ["IPC", "LLC MPKI", "LLC miss rate"]) assert.ok(text.includes(label));
  assert.ok(text.includes("historical=3"));
  assert.ok(text.includes("no_compatible_quality_gated_pmu_evidence"));
});


test("formal model outputs do not fill an absent model", () => {
  const tool = structuredClone(example);
  const trie = structuredClone(example);
  tool.targets.duration_ms.context = ["TOOL_ONLY"];
  trie.targets.duration_ms.context = ["TRIE_ONLY"];
  const text = formatCallLoadPrediction({tool: {load: tool}, trie: {load: trie}}).join("\n");
  assert.ok(text.includes("TOOL_ONLY"));
  assert.ok(text.includes("TRIE_ONLY"));
  assert.ok(text.includes("LatticeKB - prediction not supplied"));
  assert.ok(!text.includes("Selected prediction"));
});
