"""Bounded, exact memoization of pure build statistics; never a learned state.

Keys include every numerical input, its order and the applicable prior. Values
are immutable-by-convention scalar dictionaries, copied into new NodeStats.
Eviction only causes recomputation. No mutable node or observation object is retained.
"""
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

Statistics = dict[str, float | int]
CacheKey = tuple[Any, ...]


class BuildStatisticsCache:
    def __init__(self, capacity: int = 16384) -> None:
        self.capacity = capacity
        self._values: OrderedDict[CacheKey, Statistics] = OrderedDict()

    def fork(self) -> "BuildStatisticsCache":
        result = type(self)(self.capacity)
        result._values = self._values.copy()
        return result

    def get_or_compute(self, key: CacheKey, compute: Callable[[], Statistics]) -> Statistics:
        if key in self._values:
            self._values.move_to_end(key)
            return self._values[key]
        result = compute()
        if self.capacity:
            self._values[key] = result
            if len(self._values) > self.capacity:
                self._values.popitem(last=False)
        return result
