"""Algorithm accelerations preserve draws, parent policy and build state exactly."""
import copy
import json
import math
import random
from dataclasses import asdict, replace
from itertools import combinations

import pytest

from clawtune_sidecar.predictors import call_load
from tool_resource.runtime_kb import ClauseObservation
from tool_time._lattice_vendor.features import CoverageIndex
from tool_time._lattice_vendor.schemas import NodeStats
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances, find_immediate_parents
from tool_time.build_cache import BuildStatisticsCache
from tool_time.lattice_kb import LatticeTimeKB, _build_node_state


@pytest.mark.parametrize("clause_count", [2, 3])
def test_composition_preserves_every_draw_and_rng_state(monkeypatch, clause_count):
    populations = [tuple(float(i * i) for i in range(1, 31 + j)) for j in range(clause_count)]
    old_random = random.Random
    expected_rng = old_random(0)
    expected_draws = [[expected_rng.choice(v) for v in populations] for _ in range(2048)]
    draws, instances = [], []
    class RecordedRandom(old_random):
        def __init__(self, seed):
            super().__init__(seed)
            instances.append(self)
        def choice(self, population):
            value = super().choice(population)
            draws.append(value)
            return value
    monkeypatch.setattr(call_load.random, "Random", RecordedRandom)
    evidence = [{"duration_ms": {"values": v}} for v in populations]
    result = call_load.compose("lattice", evidence, {target: [100, 500] for target in call_load.TARGET_UNITS})
    assert draws == [v for row in expected_draws for v in row]
    assert instances[0].getstate() == expected_rng.getstate()
    expected = call_load.summarize("duration_ms", [100, 500], "lattice",
                                   [sum(row) for row in expected_draws])
    actual = result.targets["duration_ms"]
    assert (actual.p50, actual.p90, actual.buckets) == (expected.p50, expected.p90, expected.buckets)


def old_parents(fs, nodes):
    for size in range(len(fs) - 1, -1, -1):
        found = [frozenset(c) for c in combinations(list(fs), size) if frozenset(c) in nodes]
        if found:
            return found
    return []


def test_indexed_parents_preserve_maximum_layer_and_combination_order():
    rng = random.Random(42)
    universe = [str(i) for i in range(12)]
    nodes = {frozenset(rng.sample(universe, rng.randrange(5))): None for _ in range(100)}
    index = CoverageIndex(nodes)
    for size in range(1, 13):
        for _ in range(10):
            query = frozenset(rng.sample(universe, size))
            assert find_immediate_parents(query, nodes, index=index) == old_parents(query, nodes)
    # Preserve the current layer policy, NOT every inclusion-maximal subset.
    query = frozenset(universe)
    sparse = {frozenset({'0', '1', '2'}): None, frozenset({'3', '4'}): None}
    assert find_immediate_parents(query, sparse, index=CoverageIndex(sparse)) == [frozenset({'0', '1', '2'})]


def test_precomputed_sample_std_is_bit_exact():
    import statistics
    rng = random.Random(2)
    nodes = {}
    for size in range(5):
        for features in combinations('abcd', size):
            values = [rng.choice([0., 1e-12, 1., 1e12]) for _ in range(rng.randrange(1, 12))]
            std = statistics.stdev([math.log1p(v) for v in values]) if len(values) > 1 else .5
            fs = frozenset(features)
            nodes[fs] = NodeStats(fs, durations=values, count=len(values), std_log=std)
    reference = copy.deepcopy(nodes)
    compute_shrinkage_variances(reference, global_log_var=.25)
    compute_shrinkage_variances(nodes, global_log_var=.25, index=CoverageIndex(nodes), reuse_sample_std=True)
    assert nodes == reference


def row(i, argv=('python', '-m', 'pytest'), **kwargs):
    return ClauseObservation('repo', argv[0], argv, i * 10., i * 10. + 1.,
                             latency_ms=float(i + 1) * 100, **kwargs)


def exact_state(state):
    nodes, var, std, median, resources, _ = state
    def dump(mapping):
        return [(tuple(sorted(fs)), {**asdict(node), 'features': sorted(node.features)})
                for fs, node in mapping.items()]
    return json.dumps((dump(nodes), var, std, median,
            [(key, view.global_log_var, view.global_log_std, dump(view.nodes))
             for key, view in resources.items()]), sort_keys=True)


@pytest.mark.parametrize('subset_coverage', [False, True])
def test_cross_generation_cache_equals_uncached_rebuild(subset_coverage):
    cache = BuildStatisticsCache()
    rows = [row(0, cpu_peak_cores=0), row(1, cpu_peak_cores=2),
            row(2, ('python', '-m', 'pytest', 'a.py'), cpu_peak_cores=4)]
    for extra in [None, row(3, ('python', '-m', 'pytest', 'b.py'), cpu_peak_cores=8),
                  row(4, ('python', 'new.py'), cpu_peak_cores=16),
                  replace(row(5), latency_ms=0, cpu_peak_cores=0),
                  replace(row(6), in_loop=True, cpu_peak_cores=32)]:
        if extra is not None:
            rows.append(extra)
        expected = _build_node_state(rows, subset_coverage=subset_coverage)
        actual = _build_node_state(rows, subset_coverage=subset_coverage, statistics_cache=cache)
        assert exact_state(actual) == exact_state(expected)
    assert cache._values


def test_statistics_cache_fork_isolated_and_does_not_change_causal_visibility():
    kb = LatticeTimeKB.fit([row(0), row(1)])
    kb.prepare()
    before = list(kb._statistics_cache._values.items())
    successor = kb.fork_for_update()
    successor.observe_completed_clause(row(2))
    successor.observe_completed_clause(row(4))
    successor.prepare()
    assert list(kb._statistics_cache._values.items()) == before
    for ts, rows in [(21., [row(0), row(1)]), (22., [row(0), row(1), row(2)]),
                     (42., [row(0), row(1), row(2), row(4)])]:
        successor._advance(ts)
        successor._ensure_nodes()
        expected = _build_node_state(rows)
        actual = (successor._nodes, successor._global_log_var, successor._global_log_std,
                  successor._global_median_s, successor._resource_states, successor._query_index)
        assert exact_state(actual) == exact_state(expected)


def test_successor_reuses_unchanged_statistics_without_reusing_changed_prior():
    cache = BuildStatisticsCache()
    history = [row(0), row(1), row(2, ('python', 'single.py'))]
    _build_node_state(history, statistics_cache=cache)
    previous = dict(cache._values)
    computations = []
    hits = []
    original = cache.get_or_compute
    def counted(key, compute):
        if key in previous:
            hits.append(key)
        def tracked():
            computations.append(key)
            return compute()
        return original(key, tracked)
    cache.get_or_compute = counted
    _build_node_state([*history, row(100, ('git', 'status'))], statistics_cache=cache)
    assert computations
    assert any(key[2] is None for key in hits if key[0] == 'time')
    # Multi-sample statistics are reusable; singleton standard errors depend on
    # the new global prior and must get distinct cache keys.
    assert not any(key in previous for key in computations)
    assert any(key[0] == 'time' and key[2] is not None for key in computations)


@pytest.mark.parametrize('capacity', [0, 1, 32])
def test_cache_eviction_and_changed_groups_and_prior_preserve_exact_statistics(capacity):
    cache = BuildStatisticsCache(capacity)
    base = [replace(row(0), latency_ms=1000., cpu_peak_cores=-0.0),
            replace(row(1), latency_ms=1000., cpu_peak_cores=0.0)]
    cases = [base,
             [base[0], replace(base[1], argv=('python', '-m', 'pytest', 'a.py'))],
             [*base, row(3, ('python', 'other.py'))],
             [replace(base[0], cpu_peak_cores=0.0), replace(base[1], cpu_peak_cores=-0.0)],
             [replace(base[0], memory_total_peak_bytes=1024, memory_extra_peak_bytes=1024,
                      memory_baseline_bytes=0, memory_eligible=True,
                      memory_environment_id='test', memory_measurement='cgroup_v2_environment_union_v1')]]
    for rows in [*cases, *reversed(cases)]:
        assert exact_state(_build_node_state(rows, statistics_cache=cache)) == exact_state(_build_node_state(rows))
        assert len(cache._values) <= capacity
