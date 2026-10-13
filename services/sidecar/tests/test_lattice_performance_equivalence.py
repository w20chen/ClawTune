"""Performance changes must preserve candidates, statistics and causal visibility."""
import math
import random
import statistics
from dataclasses import asdict, replace

import pytest

from command_parser.models import CommandFeatures
from tool_resource.runtime_kb import ClauseObservation
from tool_time import lattice_kb
from tool_time._lattice_vendor.features import CoverageIndex
from tool_time._lattice_vendor.nodes import _loo_mse_log
from tool_time._lattice_vendor.schemas import NodeStats
from tool_time.query import query_nodes
from tool_time.resource_lattice import build_resource_states


def test_index_preserves_full_scan_order_core_and_exact_own_view():
    rng = random.Random(42)
    universe = [f"arg:{i}" for i in range(16)]
    nodes = {}
    for i in range(1500):
        features = frozenset([f"tool=t{i % 3}", *rng.sample(universe, rng.randrange(10))])
        nodes[features] = NodeStats(features, durations=[1., 4.], count=2,
                                    own_durations=[4.] if i % 3 == 0 else [])
    index = CoverageIndex(nodes)
    queries = list(nodes)[:150] + [frozenset(['tool=t0', *rng.sample(universe, 12)]) for _ in range(150)]
    for features in queries:
        tool = next(f for f in features if f.startswith('tool='))
        core = frozenset([tool, *sorted(features - {tool})[:1]])
        query = CommandFeatures(tool[5:], features, core, 'test')
        expected = query_nodes(nodes, query, global_log_std=.5)
        actual = query_nodes(nodes, query, global_log_std=.5, index=index)
        assert list(actual.items()) == list(expected.items())
    assert list(CoverageIndex([frozenset(), *nodes]).subsets(frozenset())) == [frozenset()]


@pytest.mark.parametrize('n', [0, 1, 2, 3, 4, 5, 20, 101, 1000])
def test_loo_median_is_bit_exact_to_original_slice_formula(n):
    rng = random.Random(n)
    values = [math.log1p(rng.choice([0., .001, 1., 10., 1e12])) for _ in range(n)]
    ordered = sorted(values)
    errors = []
    for i, value in enumerate(ordered):
        if n > 1:
            rest = ordered[:i] + ordered[i + 1:]
            errors.append((value - statistics.median(rest)) ** 2)
    assert _loo_mse_log(values) == (statistics.mean(errors) if errors else 0.)


def observation(index, **kwargs):
    return ClauseObservation('r', 'python', ('python', 'run.py'), index, index + .5,
                             latency_ms=1000. * (index + 1), **kwargs)


@pytest.mark.parametrize('subset_coverage', [False, True])
def test_cached_target_states_equal_independent_builds(subset_coverage):
    rows = [observation(0, cpu_ns_cumulative=0, cpu_peak_cores=0),
            observation(1, cpu_ns_cumulative=2e9, cpu_peak_cores=2),
            observation(2, cpu_peak_cores=7, in_loop=True),
            replace(observation(3, cpu_peak_cores=0), latency_ms=0)]
    cache = {}
    for load in (False, True):
        expected = build_resource_states(rows, load=load, subset_coverage=subset_coverage)
        actual = build_resource_states(rows, load=load, subset_coverage=subset_coverage, state_cache=cache)
        for target in expected:
            assert actual[target].global_log_std == expected[target].global_log_std
            assert list(actual[target].nodes.items()) == list(expected[target].nodes.items())
    # CPU average and cumulative targets have distinct per-row qualifications.
    assert cache


def test_fork_reuses_only_completed_state_and_builds_successor_once(monkeypatch):
    published = lattice_kb.LatticeTimeKB.fit([observation(0)])
    published.prepare()
    before = [asdict(node) for node in published._nodes.values()]
    calls = []
    build = lattice_kb._build_node_state
    def tracked(rows, **kwargs):
        calls.append(len(rows))
        return build(rows, **kwargs)
    monkeypatch.setattr(lattice_kb, '_build_node_state', tracked)
    successor = published.fork_for_update()
    successor.observe_completed_clause(observation(2))
    successor.observe_completed_clause(observation(4))
    successor.prepare()
    successor.prepare()
    assert calls == []  # An already-built generation is maintained incrementally.
    clauses = [{'bin': 'python', 'argv': ['python', 'run.py']}]
    def values(kb, ts):
        return kb.predict_load_samples('r', clauses, ts)[0]['duration_ms']['values']
    assert values(successor, 2.5) == (1000.,)  # Equality is not yet visible.
    assert values(successor, 3) == (1000., 3000.)  # Only part of pending is visible.
    assert calls == []
    assert values(successor, 5) == (1000., 3000., 5000.)
    assert calls == []  # Prepared final state is still usable after partial advance.
    assert values(published, 1) == (1000.,)
    assert [asdict(node) for node in published._nodes.values()] == before


def test_dirty_fork_and_new_observation_invalidate_prepared_successor(monkeypatch):
    original = lattice_kb.LatticeTimeKB.fit([observation(0)])
    successor = original.fork_for_update()  # No derived state exists yet.
    successor.observe_completed_clause(observation(1))
    successor.prepare()
    successor.observe_completed_clause(observation(2))
    successor.prepare()
    clauses = [{'bin': 'python', 'argv': ['python', 'run.py']}]
    assert successor.predict_load_samples('r', clauses, 4)[0]['duration_ms']['values'] == (1000., 2000., 3000.)
    assert original.predict_load_samples('r', clauses, 4)[0]['duration_ms']['values'] == (1000.,)
