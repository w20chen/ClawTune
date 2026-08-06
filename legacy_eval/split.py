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


__all__ = ["split_tasks"]
