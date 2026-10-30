"""Shared work must preserve evidence, causality, metadata and oracle objectives."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from clawtune_sidecar.prediction_config import load_bucket_edges
from clawtune_sidecar.predictors.call_load import CompositionCache, compose, duration_p50
from command_parser import ParseContext, default_service
from tool_time import query as query_module
from tool_time.lattice_kb import LATTICE_TIME_ALGORITHMS, LatticeTimeKB
from tool_time.query import LatticeQueryCache
from tool_time.resource_lattice import ResourceState
from test_lattice_resources import observation


def test_resource_variants_share_candidates_without_changing_predictions():
    kb = LatticeTimeKB.fit([observation(i, latency_ms=100 + i * 70) for i in range(5)])
    kb.freeze()
    argv = ('python', 'work.py')
    features = default_service().argv(argv, ParseContext(repo='org/repo'))
    state = kb._resource_states['load:duration_ms']
    expected = [state.predict('org/repo', argv, 'duration_ms', algorithm, command_features=features)
                for algorithm in LATTICE_TIME_ALGORITHMS]
    cache = LatticeQueryCache()
    with patch.object(query_module, 'query_nodes', wraps=query_module.query_nodes) as lookup:
        actual = [state.predict('org/repo', argv, 'duration_ms', algorithm,
                                command_features=features, query_cache=cache)
                  for algorithm in LATTICE_TIME_ALGORITHMS]
        assert lookup.call_count == 1
    assert actual == expected
    assert len(cache.summaries) == 1


def test_shared_cache_separates_updated_generations_and_memory_measurements():
    kb = LatticeTimeKB.fit([
        observation(0, memory_total_peak_bytes=100),
        observation(1, memory_total_peak_bytes=900, memory_measurement='guest_memtotal_minus_memavailable'),
    ])
    clauses = [{'bin': 'python', 'argv': ['python', 'work.py']}]
    cache = LatticeQueryCache()
    first = kb.predict_load_samples('org/repo', clauses, 10, query_cache=cache)[0]
    guest = kb.predict_load_samples('org/repo', [dict(clauses[0], memory_measurement='guest_memtotal_minus_memavailable')],
                                    10, query_cache=cache)[0]
    assert first['memory_total_peak_bytes']['values'] == (100,)
    assert guest['memory_total_peak_bytes']['values'] == (900,)
    kb.observe_completed_clause(observation(11, memory_total_peak_bytes=200))
    kb.prepare()
    # Pending observations are not visible at their exact completion time.
    at_completion = kb.predict_load_samples('org/repo', clauses, 12, query_cache=cache)[0]
    assert at_completion == first
    updated = kb.predict_load_samples('org/repo', clauses, 13, query_cache=cache)[0]
    assert updated['memory_total_peak_bytes']['values'] == (100, 200)


def test_load_samples_do_not_compute_discarded_resource_quantiles(monkeypatch):
    kb = LatticeTimeKB.fit([observation()])
    def fail(*args, **kwargs):
        raise AssertionError('load evidence must not generate a resource report')
    monkeypatch.setattr(ResourceState, 'predict', fail)
    row = kb.predict_load_samples('org/repo', [{'bin': 'python', 'argv': ['python', 'work.py']}], 10)[0]
    assert row['duration_ms']['values'] == (1000,)


@pytest.mark.parametrize('algorithm', LATTICE_TIME_ALGORITHMS)
def test_exact_load_samples_skip_lazy_variances_after_update(algorithm):
    from tool_time.incremental import _NodeMap

    full = ('python', '-m', 'pytest', '-q')
    part = (*full, 'tests/test_one.py')
    rows = [observation(0, argv=full, latency_ms=80_000),
            observation(1, argv=part, latency_ms=1_000)]
    added = observation(10, argv=full, latency_ms=100_000)
    kb = LatticeTimeKB.fit(rows)
    kb.prepare()
    kb.observe_completed_clause(added)
    kb.prepare()
    kb._advance(12)
    kb._ensure_nodes()
    assert isinstance(kb._resource_states['load:duration_ms'].nodes, _NodeMap)

    reference = LatticeTimeKB.fit([*rows, added])
    clauses = [{'bin': 'python', 'argv': list(full)}]
    expected = reference.predict_load_samples('org/repo', clauses, 12, algorithm=algorithm)
    with patch.object(_NodeMap, '__getitem__', side_effect=AssertionError(
            'exact load evidence must not resolve inherited variances')):
        actual = kb.predict_load_samples('org/repo', clauses, 12, algorithm=algorithm)
    assert actual == expected
    assert actual[0]['duration_ms']['values'] == (80_000, 100_000)

    # A generalized query still needs coverage samples, including descendants.
    clauses[0]['argv'].append('-x')
    actual = kb.predict_load_samples('org/repo', clauses, 12, algorithm=algorithm)
    assert actual == reference.predict_load_samples('org/repo', clauses, 12, algorithm=algorithm)
    if algorithm == 'max_cardinality':
        assert actual[0]['duration_ms']['values'] == (80_000, 1_000, 100_000)


@pytest.mark.parametrize('pipeline', [False, True])
def test_composition_reuses_draws_but_preserves_backend_context_and_assumptions(pipeline):
    import clawtune_sidecar.predictors.call_load as module
    edges = load_bucket_edges((100, 500, 2000, 10000))
    clauses = [{'argv': ['a'], 'in_pipe': pipeline, 'pipeline_position': 0},
               {'argv': ['b'], 'in_pipe': pipeline, 'pipeline_position': 1}]
    evidence = [{target: {'values': (1., 10., 100.), 'context': ['first']} for target in edges},
                {target: {'values': (2., 20., 200.), 'context': ['second']} for target in edges}]
    changed = [{target: {**row, 'context': ['different'], 'assumptions': ['different_algorithm']}
                for target, row in item.items()} for item in evidence]
    expected = compose('lattice', changed, edges, clauses=clauses)
    cache = CompositionCache()
    with patch.object(module.random, 'Random', wraps=module.random.Random) as rng:
        compose('trie', evidence, edges, clauses=clauses, cache=cache)
        first_count = rng.call_count
        actual = compose('lattice', changed, edges, clauses=clauses, cache=cache)
        assert rng.call_count == first_count == len(edges)
    assert actual == expected


@pytest.mark.parametrize('pipeline', [False, True])
def test_oracle_objective_matches_full_composer_with_random_samples(pipeline):
    import random
    rng = random.Random(42)
    for count in (1, 2, 4):
        clauses = [{'argv': [str(i)], 'in_pipe': pipeline, 'pipeline_position': i} for i in range(count)]
        values = [tuple(rng.random() * 1000 for _ in range(13)) for _ in clauses]
        evidence = [{'duration_ms': {'values': v}} for v in values]
        expected = compose('lattice', evidence, load_bucket_edges((100, 500)), clauses=clauses)
        assert duration_p50(values, clauses) == expected.targets['duration_ms'].p50


def test_query_cache_separates_execution_core_constraints():
    kb = LatticeTimeKB.fit([observation()])
    kb.freeze()
    state = kb._resource_states['load:duration_ms']
    full = default_service().argv(('python', 'work.py'), ParseContext(repo='org/repo'))
    query = replace(full, features=full.features | {'option:extra'})
    cache = LatticeQueryCache()
    assert cache.query(state.nodes, query, global_log_std=state.global_log_std, index=state.query_index)
    restricted = replace(query, core=query.core | {'option:extra'})
    assert not cache.query(state.nodes, restricted, global_log_std=state.global_log_std, index=state.query_index)
