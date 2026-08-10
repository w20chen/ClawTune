# Review Log

This log records high-level review decisions. It intentionally stays short:
detailed change context lives in git history, and user-facing instructions are
not duplicated here.

## 2026-08-10 — documentation cleanup pass

Goal: remove redundant, low-information, and implementation-churn content from
the docs, keeping each doc idea-focused rather than code-listing.

- `getting-started.md`: the "Run SWE-Rebench" and "Run Deep Research Bench"
  sections duplicated the root README's Quick Start and the dedicated
  benchmark READMEs. Replaced with short pointers to those canonical sources.
- `legacy_eval_final_report.md`: removed the "Code changes for this
  evaluation" section (pure file/function churn) and the reproduction
  commands, which already live in `legacy-eval.md` / `legacy_eval/README.md`.
  It now points to those guides.
- `CURRENT_PLAN.md`: rewrote the DRB "Known Limitations" bullets to state the
  behavior/limitation instead of naming private implementation functions
  (`_reset_task_trace_dir`, `_link_web_search_provider_plugin`, etc.). Also
  corrected the legacy-eval description to the real
  `static_train_test_obs_per_repo` observation split and aligned the
  "Stage-2" wording with the exec-clause convention.
- `sidecar.md`: condensed the lattice-prediction paragraph that duplicated
  `trace-schema.md` field detail; it now links there.
- `configuration.md`: condensed the benchmark-KB concurrency explanation and
  the lattice KB layout detail, pointing to `swe_rebench/README.md`.

Untouched: user-facing commands, config references, troubleshooting
symptom→fix entries, and the host-only validation list in `CURRENT_PLAN.md`
(required by `AGENTS.md`).
