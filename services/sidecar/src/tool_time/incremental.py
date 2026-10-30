"""Copy-on-write maintenance of the existing Lattice node statistics.

The batch builder remains the reference and the snapshot stays a raw observation
log. Samples, signature groups, node order, parent policy and arithmetic match
that builder. Published generations and their NodeStats are never mutated.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from command_parser.service import observation_features
from kb_common.feature_index import SupersetIndex
from tool_resource.runtime_kb import is_pipeline_dependent_consumer
from tool_time._lattice_vendor.features import CoverageIndex, generate_context_nodes
from tool_time._lattice_vendor.nodes import _compute_loso_risk, _loo_mse_log
from tool_time._lattice_vendor.schemas import NodeStats
from tool_time._lattice_vendor.shrinkage import find_immediate_parents
from tool_time.resource_lattice import (
    LOAD_TARGETS,
    RESOURCE_TARGETS,
    ResourceState,
    nonnegative,
    resource_values,
)


@dataclass(frozen=True, slots=True)
class _Sample:
    order: tuple
    features: frozenset
    core: frozenset
    value: float


def _view_samples(rows, offset=0):
    from tool_time.lattice_kb import _observation_sort_key
    output = defaultdict(list)
    for i, row in enumerate(rows, offset):
        record = observation_features(row)
        order = (_observation_sort_key(row), i)
        def add(name, value, *, order=order, record=record):
            output[name].append(_Sample(order, record.features, record.core, value))
        if nonnegative(row.latency_ms) and row.latency_ms > 0:
            add('time', float(row.latency_ms) / 1000.0)
        measured = resource_values(row)
        load_ok = not (row.in_loop or row.in_subst or is_pipeline_dependent_consumer(row))
        if load_ok and nonnegative(row.latency_ms):
            add('load:duration_ms', float(row.latency_ms) / 1000.0)
        for target, value in measured.items():
            suffix = ':' + (row.memory_measurement or 'cgroup_v2_environment_union_v1') if target.startswith('memory_') else ''
            add(target + suffix, value / RESOURCE_TARGETS[target][1])
            if load_ok:
                add('load:' + target + suffix, value / RESOURCE_TARGETS[target][1])
        # Empty measured views are part of the existing state too.
        measurement = row.memory_measurement or 'cgroup_v2_environment_union_v1'
        for target in RESOURCE_TARGETS:
            if target.startswith('memory_'):
                output[target + ':' + measurement]
                output['load:' + target + ':' + measurement]
    output['time']
    for target in RESOURCE_TARGETS:
        if not target.startswith('memory_'):
            output[target]
            output['load:' + target]
    output['load:duration_ms']
    return output


@lru_cache(maxsize=4096)
def _group_mean(values):
    return statistics.mean(math.log1p(v) for v in values)


@lru_cache(maxsize=4096)
def _group_median(values):
    return math.log1p(statistics.median(values))


@lru_cache(maxsize=4096)
def _statistics(values, groups, prior, legacy):
    """Same operation order as nodes.py/resource_lattice.py, including zeros."""
    logs = [math.log1p(v) for v in values]
    std = statistics.stdev(logs) if len(logs) > 1 else prior
    means = [_group_mean(group) for group in groups]
    return dict(  # noqa: C408 - keep the scalar formula names aligned with the batch builder
        count=len(values), mean_s=statistics.mean(values), median_s=statistics.median(values),
        geometric_mean_s=(
            math.exp(sum(math.log(v) for v in values) / len(values)) if legacy else
            math.exp(statistics.mean(math.log(v) for v in values)) if all(v > 0 for v in values) else 0.0),
        mean_log=statistics.mean(logs), std_log=std, stderr_log=std / math.sqrt(len(values)),
        loo_mse_log=_loo_mse_log(logs, estimator='median'), signature_count=len(groups),
        signature_log_mean_var=(
            (statistics.stdev(means) ** 2 if legacy else statistics.variance(means)) if len(means) > 1 else 0.0),
        loso_risk=_compute_loso_risk(dict(enumerate(groups)), log_median=_group_median),
    )


def _shrinkage(node, parents, nodes, global_var, kappa):
    own = node.std_log ** 2 if node.count >= 2 else 0.0
    parent_vars = [nodes[p].shrinkage_var for p in parents if not nodes[p].cold_start]
    if not parent_vars:
        return (own, False) if node.count >= 2 else (global_var, True)
    prior = statistics.median(parent_vars)
    return ((prior if node.count == 1 else
             ((node.count - 1) * own + kappa * prior) / ((node.count - 1) + kappa)), False)


class _NodeMap(Mapping):
    """Memoize the unchanged shrinkage formula for this immutable generation.

    Updating the global prior can affect every singleton. Computing those
    millions of derived scalars eagerly defeats incremental maintenance. Raw
    samples and own statistics are ready at publication; reads resolve exactly
    the same parent DAG, with no approximation or stale prior. Iteration order
    and every returned NodeStats field match an eager full build.
    """

    def __init__(self, raw, parents, std, kappa, order=None):
        self.raw, self.parents, self.std, self.kappa = raw, parents, std, kappa
        self.resolved = {}
        self.order, self.ordered_keys = order, None

    def __len__(self):
        return len(self.raw)

    def __iter__(self):
        if self.order is None:
            return iter(self.raw)
        if self.ordered_keys is None:
            first, generation = self.order
            self.ordered_keys = tuple(sorted(self.raw, key=lambda fs: (first[fs], generation[fs])))
        return iter(self.ordered_keys)

    def __contains__(self, key):
        return key in self.raw

    def raw_get(self, key, default=None):
        node = self.raw.get(key)
        if node is None:
            return default
        # Exact-own-sample queries replace variance, but retain the cold-start
        # flag. It is cold iff neither this node nor any ancestor has >= 2
        # samples. Resolve that Boolean without computing discarded variances.
        pending, seen = [key], set()
        cold = True
        while pending:
            fs = pending.pop()
            if fs in seen:
                continue
            seen.add(fs)
            if self.raw[fs].count >= 2:
                cold = False
                break
            pending.extend(self.parents[fs])
        if node.cold_start != cold:
            copied = object.__new__(NodeStats)
            copied.__dict__ = node.__dict__.copy()
            copied.cold_start = cold
            node = copied
        return node

    def __getitem__(self, key):
        if key not in self.raw:
            raise KeyError(key)
        pending = [key]
        while pending:
            fs = pending[-1]
            if fs in self.resolved:
                pending.pop()
                continue
            ps = self.parents[fs]
            missing = [p for p in ps if p not in self.resolved]
            if missing:
                pending.extend(missing)
                continue
            node = self.raw[fs]
            variance, cold = _shrinkage(node, ps, self.resolved, self.std ** 2, self.kappa)
            prior_changed = node.count == 1 and node.std_log != self.std
            if prior_changed or node.shrinkage_var != variance or node.cold_start != cold:
                copied = object.__new__(NodeStats)
                copied.__dict__ = node.__dict__.copy()
                copied.shrinkage_var, copied.cold_start = variance, cold
                if prior_changed:
                    copied.std_log = copied.stderr_log = self.std
                node = copied
            self.resolved[fs] = node
            pending.pop()
        return self.resolved[key]


class _View:
    def __init__(self, samples, nodes, index, std, *, legacy, maximum, subset_coverage):
        self.samples = tuple(sorted(samples, key=lambda row: row.order))
        self.nodes, self.index, self.std = nodes, index, std
        self.raw_nodes = nodes
        self.parents = index.parent_sets
        self.generation_order = index.generation_order
        self.first_cover = index.first_cover
        self.legacy, self.maximum, self.subset_coverage = legacy, maximum, subset_coverage

    def extend(self, added, contexts):
        from tool_time.lattice_kb import _NODE_MODE, _SHRINKAGE_KAPPA
        if not added:
            return self
        # Heap-array order need not be chronological, even when every new
        # completion is later than the previous generation's final sample.
        added = tuple(sorted(added, key=lambda row: row.order))
        samples = tuple(sorted((*self.samples, *added), key=lambda row: row.order))
        records = SupersetIndex((i, row.features) for i, row in enumerate(samples))
        def generated(row):
            key = (row.features, row.core)
            if key not in contexts:
                contexts[key] = tuple(generate_context_nodes(row.features, row.core,
                    mode=_NODE_MODE, max_optional_features=self.maximum, always_keep_exact=True))
            return contexts[key]
        new = {}
        affected = set()
        for row in added:
            own = generated(row)
            new.update(dict.fromkeys(fs for fs in own if fs not in self.nodes))
            affected.update(self.index.subsets(row.features) if self.subset_coverage else own)
        affected.update(new)
        nodes = self.raw_nodes.copy()
        logs = [math.log1p(row.value) for row in samples]
        std = statistics.stdev(logs) if len(logs) >= 2 else .5
        first_cover = self.first_cover.copy() if new else self.first_cover
        parent_changes = set(new)
        coverage_cache = {}
        added_orders = {r.order for r in added}
        for fs in affected:
            positions = sorted(records.supersets(fs))
            matching = [samples[i] for i in positions]
            if not self.subset_coverage:
                matching = [r for r in matching if fs == r.features or
                            (r.core <= fs and len(fs - r.core) <= self.maximum)]
            if fs in new:
                first_cover[fs] = (positions[0] if self.subset_coverage else next(
                    i for i in positions if fs == samples[i].features or
                    (samples[i].core <= fs and len(fs - samples[i].core) <= self.maximum)))
                # A pre-existing descendant must have been generated by an
                # old observation covering this new node. Search only those
                # signatures, rather than testing every node in every view.
                for signature in {samples[i].features for i in positions
                                  if samples[i].order not in added_orders}:
                    if signature not in coverage_cache:
                        coverage_cache[signature] = tuple(self.index.subsets(signature))
                    for child in coverage_cache[signature]:
                        ps = self.parents.get(child, ())
                        if fs < child and len(fs) >= (len(ps[0]) if ps else -1):
                            parent_changes.add(child)
            groups = defaultdict(list)
            for row in matching:
                groups[row.features].append(row.value)
            values = tuple(row.value for row in matching)
            nodes[fs] = NodeStats(fs, durations=list(values), own_durations=list(groups.get(fs, ())),
                **_statistics(values, tuple(tuple(v) for v in groups.values()), std if len(values) == 1 else None, self.legacy))

        # The selector uses stable insertion order for ties. A new node can
        # cover old samples, and late delivery can reorder groups. Reproduce
        # the batch builder's node order without rebuilding its statistics.
        late = bool(self.samples and min(r.order for r in added) < self.samples[-1].order)
        generation_order = self.generation_order
        if late:
            generated_order = {}
            for row in samples:
                generated_order.update(dict.fromkeys(generated(row)))
            existence = CoverageIndex(generated_order)
            generation_order = {fs: i for i, fs in enumerate(generated_order)}
            first_cover = {}
            ordered = {}
            seen = set()
            for position, row in enumerate(samples):
                key = row.features if self.subset_coverage else (row.features, row.core)
                if key in seen:
                    continue
                seen.add(key)
                for fs in existence.subsets(row.features) if self.subset_coverage else generated(row):
                    first_cover.setdefault(fs, position)
                    ordered.setdefault(fs, nodes[fs])
            nodes = ordered
            index = CoverageIndex(nodes)
        elif new:
            generation_order = self.generation_order.copy()
            generation_order.update((fs, len(self.generation_order) + i) for i, fs in enumerate(new))
            index = self.index.fork_added(new, first_cover, generation_order)
        else:
            index = self.index
        parents = self.parents.copy() if new else self.parents
        for fs in parent_changes:
            parents[fs] = tuple(nodes[p].features for p in find_immediate_parents(fs, nodes, index=index))
        if index is not self.index:
            index.parent_sets = parents
            index.first_cover = first_cover
            index.generation_order = generation_order
        result = object.__new__(_View)
        result.__dict__ = {**self.__dict__, 'samples': samples,
                           'nodes': _NodeMap(nodes, parents, std, _SHRINKAGE_KAPPA, index._order),
                           'raw_nodes': nodes, 'index': index, 'std': std, 'parents': parents,
                           'generation_order': generation_order, 'first_cover': first_cover}
        return result


class IncrementalLatticeState:
    def __init__(self, rows, state, *, maximum, subset_coverage):
        self.rows = tuple(rows)
        self.contexts = {}
        training = _view_samples(rows)
        nodes, _, std, _, resources, index = state
        self.views = {'time': _View(training.pop('time'), nodes, index, std,
            legacy=True, maximum=maximum, subset_coverage=subset_coverage)}
        shared = {}
        for name, view in resources.items():
            if id(view) not in shared:
                shared[id(view)] = _View(training[name], view.nodes, view.query_index, view.global_log_std,
                    legacy=False, maximum=maximum, subset_coverage=subset_coverage)
            self.views[name] = shared[id(view)]

    def extend(self, rows):
        # Raw rows are append-only. A prepared all-pending version can use a
        # different tie order from heap delivery; the owner checks this prefix.
        added = _view_samples(rows[len(self.rows):], len(self.rows))
        result = object.__new__(type(self))
        result.rows = tuple(rows)
        result.contexts = self.contexts.copy()
        result.views = {}
        shared = {}
        for name in dict.fromkeys((*self.views, *added)):
            previous = self.views.get(name)
            if previous is None:
                base = self.views['time']
                previous = _View((), {}, CoverageIndex(()), .5, legacy=False,
                    maximum=base.maximum, subset_coverage=base.subset_coverage)
            incoming = added.get(name, ())
            key = (id(previous), tuple((s.order, s.features, s.core, s.value.hex()) for s in incoming))
            if key not in shared:
                shared[key] = previous.extend(incoming, result.contexts)
            result.views[name] = shared[key]
        return result

    def state(self):
        view = self.views['time']
        resources = {}
        shared = {}
        # Match the existing public resource view order, including memory partitions.
        names = [t for t in RESOURCE_TARGETS if not t.startswith('memory_')]
        names += ['load:' + t for t in LOAD_TARGETS if not t.startswith('memory_')]
        # Partition names already identify every measurement source; do not
        # scan or sort the full observation history just to recover that set.
        measurements = {name.partition(':')[2] for name in self.views if name.startswith('memory_')}
        for measurement in measurements:
            for prefix in ('', 'load:'):
                names += [prefix + t + ':' + measurement for t in RESOURCE_TARGETS if t.startswith('memory_')]
        for name in names:
            v = self.views[name]
            if id(v) not in shared:
                shared[id(v)] = ResourceState(v.nodes, v.std ** 2, v.std, v.index)
            resources[name] = shared[id(v)]
        return (view.nodes, view.std ** 2 if view.samples else 0.0, view.std,
                statistics.median(s.value for s in view.samples) if view.samples else 0.0,
                resources, view.index)
