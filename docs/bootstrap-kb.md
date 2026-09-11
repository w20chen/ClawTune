# Bundled bootstrap KB

Daily use and every new online benchmark run start from `seeds/bootstrap-v1`
unless an explicit seed override is supplied. This release bundle contains
ToolKB, TrieKB and LatticeKB and is included in both sidecar source releases
and wheels installed with the plugin. Building a wheel from a source release
does not require the parent checkout or historical Git objects.
Offline fixed-trace evaluation still trains its own bundle
only from its training split.

## Contents and selection

The source is the historical SWE-Rebench training-only clause corpus formerly
packaged as `demo-v1`. The large historical bundle and the old snapshots under
`traces/tool-resource/` are removed from the checkout. The original repository's
Git history retains them; shallow clones or rewritten histories may not.

The reproducible recipe selects **40 observations**: eight each for `cat`,
`find`, `grep`, `ls` and `which`. It excludes pipelines, loops, substitutions,
invalid duration labels, builds, test suites, package installation and arbitrary
Python programs. For each executable, it first takes one median-duration
observation per source repository, then selects eight midpoint duration
quantiles. This limits project weighting and retains a range of durations
instead of selecting only fast runs.

Before constructing either KB, the recipe clears the repository field and
replaces argv with the executable alone. No source repository names, task IDs,
paths, search patterns, exact command signatures or repository-specific nodes
are distributed. These are executable-level aggregated priors, not measurements
of actually invoking each executable without arguments. TrieKB has public priors
only; LatticeKB builds common contexts without a repository feature.

| KB / target | Initial evidence |
| --- | --- |
| TrieKB and LatticeKB duration | 40 clause observations |
| Clause CPU time and average cores | 6 observations each |
| Clause sampled peak RSS | 6 observations |
| Clause 500 ms peak CPU | Unavailable |
| ToolKB whole-call and PMU targets | Empty; accumulate online |

CPU/RSS labels are withheld for executions shorter than 20 ms because the
historical measurements are coarse or sparsely sampled. CPU must be positive
and no greater than duration times the source's 8-core quota; sampled RSS must
be at least 64 KiB. Peak CPU additionally requires at least a 500 ms window.
Missing labels are never synthesized from duration, cgroup limits or another
metric. The retained CPU/RSS labels still describe historical source conditions.

This improves input hygiene, not measured prediction accuracy: the small prior
is uncalibrated, originates only from SWE-Rebench, and cannot establish
cross-benchmark or cross-hardware accuracy. Removing identity prevents exact
repo/task matching but does not turn reused historical observations into an
independent held-out dataset. Online experiments should record this seed's
manifest hash and compare later predictions against this initial baseline.

## Reproduce and inspect

From a full Git checkout with the historical source commit available:

```bash
python scripts/build_bootstrap_seed.py --output .runtime/bootstrap-rebuilt
python -c "from pathlib import Path; a=Path('seeds/bootstrap-v1'); b=Path('.runtime/bootstrap-rebuilt'); assert all(p.read_bytes() == (b/p.name).read_bytes() for p in a.glob('*.json')); print('All four seed files match')"
python3 scripts/clawtune.py benchmark --seed .runtime/bootstrap-rebuilt --sample 1 --dry-run
```

The output must be a new directory. The script reads an immutable source Git
object; no external trace dataset is modified. `--source <snapshot.json>` can
instead supply the original finalized historical LatticeKB snapshot. The
manifest records the source SHA-256, recipe, selection counts, quality policy,
source CPU quota and hashes of all three snapshots. Rebuilding from the same
source is byte-identical. No historical data download or training occurs at
normal startup.

The default historical object is
`41e993395d82723deb7d4193467dc3bd5283574a:seeds/demo-v1/clause-lattice-time-kb.json`.
If it is unavailable, supply the original snapshot with `--source`; fetching
more history only helps if that object exists in the upstream history.
Ordinary tests use synthetic source observations to verify deterministic
selection, sanitization and rebuilding without Git history. The comparison
above separately verifies exact reproduction of the shipped bundle from the
original source. Neither installing nor testing the plugin requires that source.

## Existing installations and runs

The default changes only new KB initialization. Existing daily KBs and saved
benchmark runs keep their accumulated state. To test a fresh daily bootstrap
without deleting learned state, set `CLAWTUNE_STATE_DIR` to a new directory
before starting the sidecar. Remove or update an explicit `CLAWTUNE_KB_SEED`
override if it points to a retired bundle. A new online run gets its own copy;
`--resume` still requires the original run's seed and configuration, so old
experiments need their archived seed rather than substituting this bundle.
