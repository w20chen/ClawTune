"""Task-level random train/test split for the legacy evaluation.

Splitting is performed at the granularity of a *task* (one SWE-bench-style
instance directory), not individual tool calls.  A task is a logical unit: it
is one workspace/repo history, and ClawTune's knowledge bases are keyed per
repo.  Splitting by task therefore prevents the same repo's commands from
appearing in both training and test, which would leak the exact answer.

The split is deterministic for a given ``seed``: the task ids are sorted,
shuffled with ``random.Random(seed)``, and the first ``round(n * train_frac)``
tasks become the training set.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence

_TRAILING_PR_RE = re.compile(r"-\d+$")


def repo_prefix(task_id: str) -> str:
    """Return the ``<org>__<repo>`` prefix of a swe-rebench case id.

    ``encode__starlette-2711`` -> ``encode__starlette``.  The trailing
    ``-<pr>`` is the pull-request number; everything before it is the repo
    namespace the KB keys repo-specific evidence by.
    """

    return _TRAILING_PR_RE.sub("", task_id)


def split_tasks(
    task_ids: list[str],
    *,
    train_frac: float = 0.8,
    seed: int = 42,
) -> tuple[list[str], list[str]]:
    """Return ``(train_ids, test_ids)`` as sorted task-id lists."""

    if not 0.0 < train_frac < 1.0:
        raise ValueError(f"train_frac must be in (0, 1), got {train_frac!r}")
    ids = sorted(task_ids)
    if not ids:
        return [], []
    rng = random.Random(seed)
    rng.shuffle(ids)
    split = int(len(ids) * train_frac)
    return sorted(ids[:split]), sorted(ids[split:])


def split_tasks_by_repo(
    task_ids: list[str],
    *,
    train_frac: float = 0.8,
    seed: int = 42,
) -> tuple[list[str], list[str]]:
    """Return ``(train_ids, test_ids)`` by splitting *within each repo*.

    Tasks are grouped by their ``<org>__<repo>`` prefix.  Each repo's tasks
    are shuffled deterministically (seeded, reproducible) and the first
    ``round(n * train_frac)`` go to training, the rest to test.  A repo whose
    rounded training count equals its size contributes no test tasks (e.g.
    single-task repos are train-only), which is intended: a test task must
    belong to a repo that also has training tasks.

    Splitting at repo granularity (instead of a global shuffle) prevents a
    repo's commands from appearing in both train and test via the same
    namespace while still measuring cross-task generalization.
    """

    if not 0.0 < train_frac < 1.0:
        raise ValueError(f"train_frac must be in (0, 1), got {train_frac!r}")
    ids = sorted(task_ids)
    if not ids:
        return [], []
    by_repo: dict[str, list[str]] = {}
    for task_id in ids:
        by_repo.setdefault(repo_prefix(task_id), []).append(task_id)
    train_ids: list[str] = []
    test_ids: list[str] = []
    for repo in sorted(by_repo):
        members = sorted(by_repo[repo])
        rng = random.Random(f"{seed}:{repo}")
        rng.shuffle(members)
        split = round(len(members) * train_frac)
        train_ids.extend(members[:split])
        test_ids.extend(members[split:])
    return sorted(train_ids), sorted(test_ids)


def split_observations_by_repo(
    observations: Sequence[tuple[str, str, str]],
    *,
    train_frac: float = 0.8,
    seed: int = 42,
    min_repo_obs: int = 10,
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """latt-style observation-level split.

    Each observation is ``(repo, task_id, tool_call_id)``. Duplicate logical
    calls are collapsed before splitting. Observations are grouped by repo;
    repos with at least ``min_repo_obs`` unique observations
    contribute ``max(1, int(n * (1 - train_frac)))`` of their observations to
    the test set (seeded deterministic shuffle), the rest to training; smaller
    repos contribute everything to training.  This mirrors
    ``latt/run_full_eval.py``.  Returns ``(train_keys, test_keys)`` where each
    key is ``(task_id, tool_call_id)``.
    """

    if not 0.0 < train_frac < 1.0:
        raise ValueError(f"train_frac must be in (0, 1), got {train_frac!r}")
    # A call id is the split unit.  Multiple attempts can repeat the same
    # ``(task_id, call_id)``; deduplicate before shuffling so one logical call
    # cannot be assigned to both sides and cannot distort the repo threshold.
    by_repo: dict[str, set[tuple[str, str, str]]] = {}
    for repo, task_id, call_id in observations:
        by_repo.setdefault(repo or "unknown", set()).add((repo, task_id, call_id))
    rng = random.Random(seed)
    train_keys: set[tuple[str, str]] = set()
    test_keys: set[tuple[str, str]] = set()
    for repo in sorted(by_repo):
        members = sorted(by_repo[repo])
        if len(members) >= min_repo_obs:
            n_test = max(1, int(len(members) * (1.0 - train_frac)))
            rng.shuffle(members)
            for _, task_id, call_id in members[:n_test]:
                test_keys.add((task_id, call_id))
            for _, task_id, call_id in members[n_test:]:
                train_keys.add((task_id, call_id))
        else:
            for _, task_id, call_id in members:
                train_keys.add((task_id, call_id))
    return train_keys, test_keys


__all__ = ["repo_prefix", "split_observations_by_repo", "split_tasks", "split_tasks_by_repo"]
