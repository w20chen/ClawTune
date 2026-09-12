import type {CallLoadPrediction, LoadEstimate, LoadTarget, PmuTarget, ToolDecision} from "./contracts.js";

const targets: LoadTarget[] = [
  "duration_ms", "cpu_time_seconds", "cpu_avg_cores", "cpu_peak_cores", "memory_peak_rss_bytes",
];
const labels: Record<LoadTarget, string> = {
  duration_ms: "Duration", cpu_time_seconds: "CPU time", cpu_avg_cores: "CPU average",
  cpu_peak_cores: "CPU peak", memory_peak_rss_bytes: "Peak RSS",
};
const clean = (value: string): string => value.replace(/[\x00-\x1f\x7f]/g, " ");

const timeEdges = [100, 500, 2000, 10000];
const timeRanges = ["[0,100ms)", "[100,500ms)", "[500ms,2s)", "[2,10s)", "[10s,+inf)"];
function bucket(id: number | null | undefined): string {
  return id != null && Number.isInteger(id) && id >= 0 && id < timeRanges.length
    ? `#${id} ${timeRanges[id]}` : "unavailable";
}
function pointBucket(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "unavailable";
  const id = timeEdges.findIndex(edge => ms < edge);
  return bucket(id < 0 ? timeEdges.length : id);
}
function modalBucket(estimate: LoadEstimate | undefined): string {
  if (estimate?.status !== "available") return "unavailable";
  const {edges, probabilities} = estimate.buckets;
  if (edges.length !== timeEdges.length || edges.some((v, i) => v !== timeEdges[i]) ||
      !probabilities || probabilities.length !== timeRanges.length ||
      probabilities.some(v => !Number.isFinite(v) || v < 0) || !probabilities.some(v => v > 0)) return "unavailable";
  const max = Math.max(...probabilities);
  return probabilities.flatMap((p, i) => p === max ? [bucket(i)] : []).join(" / ");
}

/** Display each baseline's scope; never sum clause point estimates into a call prediction. */
export function formatTimeBuckets(prediction: ToolDecision["prediction"], callId: string): string[] {
  const lines = [`Time buckets | ${clean(callId)}`, `${"Baseline".padEnd(26)} ${"Scope".padEnd(10)} Bucket`];
  const row = (name: string, scope: string, value: string): void => {
    lines.push(`${name.padEnd(26)} ${scope.padEnd(10)} ${value}`);
  };
  for (const backend of ["runtime", "trie", "lattice"] as const) {
    row(`${backend} (mode)`, "call", modalBucket(prediction.diagnostics?.backends[backend]?.targets.duration_ms));
  }
  const resource = prediction.tool_resource;
  row("clause_latency_bucket", "command", bucket(resource?.prediction?.bucket_id));
  row("runtime (p90)", "call", pointBucket(resource?.continuous_predictions?.latency_ms?.conditional_p90));
  const clauses = resource?.lattice_time_predictions ?? [];
  for (const algorithm of ["shrinkage", "loso", "max_cardinality"] as const) {
    if (!clauses.length) row(`lattice_${algorithm}`, "clause", "unavailable");
    for (const clause of clauses) {
      const value = clause.predictions.find(p => p.algorithm === algorithm)?.prediction_ms;
      row(`lattice_${algorithm}`, `clause ${clause.clause_index}`, pointBucket(value));
    }
  }
  return lines;
}
const number = (value: number | null, scale = 1): string =>
  value === null ? "-" : Number((value / scale).toPrecision(6)).toString();

function formatEstimate(target: LoadTarget, estimate: LoadEstimate): string[] {
  const scale = target === "memory_peak_rss_bytes" ? 1024 ** 2 : 1;
  const unit = target === "memory_peak_rss_bytes" ? "MiB" : estimate.unit;
  const source = `${estimate.backend}/${estimate.method}`;
  const lines = [estimate.status === "available"
    ? `    ${labels[target].padEnd(12)} ${number(estimate.avg, scale).padStart(12)} ${number(estimate.p50, scale).padStart(12)} ${number(estimate.p90, scale).padStart(12)}  ${unit}  ${source}`
    : `    ${labels[target].padEnd(12)} unavailable  [${unit}; ${source}]`];
  if (estimate.unavailable_reason) lines.push(`      reason: ${clean(estimate.unavailable_reason)}`);
  lines.push(`      evidence: historical=[${estimate.evidence_counts.join(", ")}]; summary samples=${estimate.sample_count}; calibration=${estimate.calibration}`);
  const edges = estimate.buckets.edges;
  const probabilities = estimate.buckets.probabilities;
  if (probabilities) {
    lines.push(`      histogram (${unit}; left closed, right open):`);
    lines.push(`        ${probabilities.map((p, i) => {
      const lower = i === 0 ? "0" : number(edges[i - 1], scale);
      const upper = i === edges.length ? "inf" : number(edges[i], scale);
      return `[${lower}, ${upper}): ${number(p * 100)}%`;
    }).join(" | ")}`);
  } else {
    lines.push(`      histogram: unavailable; edges (${unit})=[${edges.map(v => number(v, scale)).join(", ")}]`);
  }
  if (estimate.context.length) lines.push(`      context: ${estimate.context.map(clean).join(" / ")}`);
  if (estimate.assumptions.length) lines.push(`      assumptions: ${estimate.assumptions.map(clean).join("; ")}`);
  return lines;
}

function formatBackend(title: string, prediction: CallLoadPrediction): string[] {
  const lines = [
    `  ${title}`,
    `    scope=${prediction.scope}; lifecycle=${prediction.lifecycle}; CPU peak window=${prediction.cpu_peak_window_ms} ms`,
    `    ${"Target".padEnd(12)} ${"Mean".padStart(12)} ${"P50".padStart(12)} ${"P90".padStart(12)}  Unit / source`,
  ];
  for (const target of targets) lines.push(...formatEstimate(target, prediction.targets[target]));
  return lines;
}

function formatPmu(prediction: ToolDecision["prediction"]): string[] {
  const lines = ["  PMU - quality-gated ToolKB history, uncalibrated"];
  const pmu = prediction.pmu_prediction;
  if (!pmu) return [...lines, "    unavailable: prediction not supplied"];
  const labels: Record<PmuTarget, string> = {
    ipc: "IPC", llc_mpki: "LLC MPKI", llc_miss_rate: "LLC miss rate",
  };
  const order: PmuTarget[] = ["ipc", "llc_mpki", "llc_miss_rate"];
  lines.push(`    ${"Target".padEnd(14)} ${"Mean".padStart(12)} ${"P50".padStart(12)} ${"P90".padStart(12)}  Unit / source`);
  for (const target of order) {
    const estimate = pmu.targets[target];
    if (estimate.status === "available") {
      lines.push(`    ${labels[target].padEnd(14)} ${number(estimate.avg).padStart(12)} ${number(estimate.p50).padStart(12)} ${number(estimate.p90).padStart(12)}  ${estimate.unit}  runtime/direct`);
      lines.push(`      evidence: historical=${estimate.evidence_count}; calibration=${estimate.calibration}`);
      if (estimate.context.length) lines.push(`      context: ${estimate.context.map(clean).join(" / ")}`);
    } else {
      lines.push(`    ${labels[target].padEnd(14)} unavailable  [${estimate.unit}; runtime/unavailable]`);
      lines.push(`      reason: ${clean(estimate.unavailable_reason ?? "unknown")}`);
    }
  }
  return lines;
}

/** Canonical call-load and PMU results followed by every load backend. */
export function formatCallLoadPrediction(prediction: ToolDecision["prediction"]): string[] {
  const pmuLines = formatPmu(prediction);
  if (!prediction.call_prediction) return pmuLines;
  const lines = ["  CALL LOAD - empirical estimates, uncalibrated", ...formatBackend("Selected prediction", prediction.call_prediction)];
  lines.push("", ...pmuLines);
  for (const backend of ["runtime", "trie", "lattice"] as const) {
    lines.push("");
    const candidate = prediction.diagnostics?.backends[backend];
    if (candidate) lines.push(...formatBackend(`${backend.toUpperCase()} - call-level candidate`, candidate));
    else lines.push(`  ${backend.toUpperCase()} - diagnostics not supplied`);
  }
  return lines;
}
