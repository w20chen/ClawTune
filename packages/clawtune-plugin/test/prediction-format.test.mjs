import assert from "node:assert/strict";
import test from "node:test";
import {readFileSync} from "node:fs";
import {formatCallLoadPrediction} from "../dist/prediction-format.js";

const example = JSON.parse(readFileSync(new URL("../../../contracts/examples/call-load.json", import.meta.url), "utf8"));

test("prints selected results and all three backends with complete histogram/evidence details", () => {
  const prediction = {call_prediction: example, diagnostics: {backends: {}}};
  for (const backend of ["runtime", "trie", "lattice"]) {
    const candidate = structuredClone(example);
    for (const estimate of Object.values(candidate.targets)) estimate.backend = backend;
    prediction.diagnostics.backends[backend] = candidate;
  }
  const text = formatCallLoadPrediction(prediction).join("\n");
  for (const title of ["Selected prediction", "RUNTIME", "TRIE", "LATTICE"]) assert.ok(text.includes(title));
  for (const label of ["Duration", "CPU time", "CPU average", "CPU peak", "Peak RSS"]) {
    assert.equal(text.split("\n").filter(line => line.startsWith(`    ${label.padEnd(12)} `)).length, 4);
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
  const text = formatCallLoadPrediction({call_prediction: candidate}).join("\n");
  assert.match(text, /CPU peak\s+unavailable/);
  assert.ok(text.includes("reason: no compatible evidence"));
  assert.ok(text.includes("histogram: unavailable; edges (cores)="));
  assert.ok(text.includes("assumptions: independent clause durations"));
  assert.equal(text.split("diagnostics not supplied").length - 1, 3);
  assert.deepEqual(formatCallLoadPrediction({}), [
    "  PMU - quality-gated ToolKB history, uncalibrated",
    "    unavailable: prediction not supplied",
  ]);
});

test("prints all three PMU targets with evidence and unavailable reasons", () => {
  const pmu = JSON.parse(readFileSync(new URL("../../../contracts/examples/pmu-prediction.json", import.meta.url), "utf8"));
  const text = formatCallLoadPrediction({pmu_prediction: pmu}).join("\n");
  for (const label of ["IPC", "LLC MPKI", "LLC miss rate"]) assert.ok(text.includes(label));
  assert.ok(text.includes("historical=3"));
  assert.ok(text.includes("no_compatible_quality_gated_pmu_evidence"));
});
