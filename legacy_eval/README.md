# legacy_eval

`legacy_eval` is the retained evaluator for historical trace layouts and the
published SWE277 experiment. It is not the current task-held-out `offline`
workflow.

Use it when reproducing the historical observation-level split, four
clause-time algorithms, or dynamic KV-TTL results. For new fixed-trace
experiments, use:

```bash
python3 scripts/clawtune.py offline --dataset <trace-root> --rss-unit MiB
```

## Historical protocol

The evaluator reads task directories containing `attempt_N/trace.jsonl` and
`clause_telemetry.json`. It groups tool calls by repository and uses the
deterministic `static_train_test_obs_per_repo` observation split. A single task
may therefore contribute calls to both train and test. Test observations never
update the three prediction KBs.

This differs intentionally from the current `offline` command, which keeps
every task and all its attempts on only one side of a persistent task-level
split.

## Commands

Show the authoritative option list:

```bash
python -m legacy_eval --help
```

Typical smoke test:

```powershell
python -m legacy_eval --dataset <dataset-root> `
  --max-train-tasks 10 --max-test-tasks 5 --print-summary
```

Export the historical training side as a seed candidate:

```powershell
python -m legacy_eval --dataset <dataset-root> `
  --export-kb "legacy_eval\.runtime\coldstart" --skip-eval
```

Review staged exports before using them; this command does not select or
replace the default runtime seed.

The canonical reproduction commands, expected fixed-snapshot metrics, TTL
policy, and kappa sweep are in
[`docs/legacy-eval.md`](../docs/legacy-eval.md). The fixed result artifact is
[`docs/legacy_eval_final_report.md`](../docs/legacy_eval_final_report.md).
