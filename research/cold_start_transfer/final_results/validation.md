# Validation evidence

Validation date: 2026-10-11. Checkout: `8fce6b72770b2eaf8a973b82183c72477fb94c2f`.

- Independent extraction and model fitting completed successfully: **68 folds, 109,912 predictions**. `independent_rerun.log` and `independent_rerun_manifest.json` retain the executed commands, exit status and initial code hashes; `execution_environment.json` records the final analysis code and dependency versions.
- Exact reproduction: **154/154 files** match SHA-256: four source exports, all 136 fold split/prediction files, and all 14 CSVs. See `reproducibility.json`.
- Source integrity: **814 original files unchanged**; duplicate execution identities, initial training/target repository overlaps and calibration/test task overlaps: **0**. See `leakage_checks.json`.
- Independent timeline arithmetic and sampling-window checks passed for **38/38 qualified memory clauses**. See `memory_timeline_verification.json`. The disjoint-cgroup labels remain in the quarantine export and are excluded from evaluation.
- **90 tests passed**: eight research protocol tests and 82 existing cold-start/Lattice/Edge/resource tests. `compileall` and `git diff --check` passed. The three generated figures were visually inspected for labels, clipping and readability.

Commands executed from the repository root (PowerShell):

```powershell
$env:PYTHONPATH='services/sidecar/src;.'
python -m pytest research/cold_start_transfer/test_protocol.py services/sidecar/tests/test_lattice_time_kb.py services/sidecar/tests/test_lattice_resources.py services/sidecar/tests/test_edge_kappa_kb.py tests/test_cold_start.py -q -p no:cacheprovider --basetemp .runtime/cold-start-transfer-final-tests
python research/cold_start_transfer/reproduce.py --flat ../swe-rebench-original-flat-644-20260904/swe-rebench-original-flat-644-20260904 --output .runtime/cold-start-transfer-reproduction-final
python research/cold_start_transfer/verify.py --data research/cold_start_transfer/final_data --results research/cold_start_transfer/final_results --rerun .runtime/cold-start-transfer-reproduction-final
python -m compileall -q research/cold_start_transfer
git diff --check
```

The delivered folds retain previously computed predictions only after exact equality of every retained input record and split was established; the independent rerun above fits all models anew. `retained_fold_audit.json` records the excluded two invalid-memory folds. Exclusion was based on raw cgroup scope evidence, not prediction performance.

Fresh benchmark collection could not run because Docker is unavailable in Windows and WSL. The required collector command and prerequisites are documented in `docs/CURRENT_PLAN.md`; no new execution measurements are claimed.
