# Fixed-trace training and evaluation

This workflow learns prediction KBs from existing traces and evaluates them
without model calls, Docker execution or test-time updates. It does not grade
benchmark answers. For the older observation-level evaluator, see the
[historical reproduction guide](legacy-eval.md).

## Input

Use a directory of v5/v6 JSONL traces with `trace_metadata`. Each task needs
`benchmark` and `instance_id`/`task_id`; repository datasets also need `repo`
or a standard repository task ID. The online runner writes adjacent
`dataset-task.json` identity records. Non-repository grouping uses `category`
when present, otherwise the dataset. Historical `bfcl-*` variants normalize to
`bfcl`, and `swebench_verified` to `swe-bench-verified`.

```bash
python3 scripts/clawtune.py offline --dataset /data/fixed-traces --rss-unit MiB
# Identity fallback for legacy SWE traces:
python3 scripts/clawtune.py offline --dataset /data/swe-traces \
  --benchmark swe-rebench --rss-unit MiB --train-fraction 0.8 --seed 42
# Traces collected by an online run:
python3 scripts/clawtune.py offline --dataset /path/to/run/traces --rss-unit MiB
```

`--benchmark` supplies missing identity or selects one dataset from mixed input.
`--rss-unit MB|MiB` is required: use the historical trace's actual unit, not a
value chosen to improve metrics. Explicit byte-valued resources keep their
byte units. Missing/invalid resource labels remain unavailable, not zero.

Pass the **task trace subtree** of an online run, not its whole output: the
sidecar directory contains additional shared diagnostic copies. The loader
keeps all attempts of a task together, prefers canonical `trace.jsonl` over
sibling raw copies within an attempt, and records exclusions. Separate flat
files without attempt metadata can represent separate attempts.

## Split and state

The default `--train-fraction 0.8` must be strictly between 0 and 1. Within
each benchmark/group, task names are deterministically hash-ordered using
`--seed` (default 42). Training receives `max(1, floor(fraction*N))` tasks;
the rest are testing tasks. Singleton groups are train-only, so the overall
fraction need not equal the requested fraction and small inputs can have no
held-out tasks.

The split registry defaults to `.runtime/offline/splits`; use
`--split-cache-dir` to move it outside the read-only input. Assignments are
reused by task roster, seed and fraction. Current source paths/hashes are
recorded independently from the logical assignment. Mixed benchmarks train
and evaluate independently; their KBs are not merged.

## Output and interpretation

`--output` names a new experiment directory; the default is
`.runtime/offline/<experiment>/`. A single dataset writes:

| Artifact | Meaning |
| --- | --- |
| `split.json` | Task membership, source hashes, grouping and registry identity |
| `seed/` | Immutable trained KB snapshots |
| `predictions.jsonl` | Held-out predictions and eligible labels |
| `report.json`, `report.md` | Coverage, errors, baselines and exclusions |

Mixed inputs have one subdirectory per benchmark and an aggregate report.
Verify train/test task counts and `test_updates: 0` before interpreting scores.
Reports include continuous errors, buckets, task-macro summaries, PMU metrics,
per-repository breakdowns and a train-only baseline where labels permit them.
Low error on a small covered subset is not evidence of broad coverage.

Call-duration labels are available more widely than clause CPU/RSS/PMU labels.
Legacy shared-cgroup measurements are not automatically accepted as isolated
per-call resource labels. The shared prediction/eligibility implementation is
used for queries; test outcomes never update the trained seed.
